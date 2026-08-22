"""Gift cards: issue one when a gift order is paid, redeem it exactly once.

The buyer and the user are different people here — an adult child pays, a
parent redeems — so the card is bearer-style: no recipient is named at
purchase, and whoever types the code gets the points.

Two guarantees, both enforced by the database rather than by memory (the app
runs multiple workers):

  1. **One card per paid order.** Cards are minted only from a verified
     payment callback, inside that callback's transaction, and `order_id` is
     unique — a retried callback cannot mint a second card.
  2. **One redemption per card.** The row is locked, `redeemed_at` is checked
     under that lock, and the ledger entry is written in the same transaction.
     Two people typing the same code at once means one wins, one is told it
     was already used.

Codes use Crockford base32: no I, L, O or U, so there is no O/0 or l/1 ambiguity
to trip over, and typed I/L/O are folded back to 1/0 anyway. That matters more
than usual — the person typing the code is often 80 and reading it off a phone
screen their grandchild sent.
"""
import secrets
from collections import namedtuple
from datetime import datetime, timezone

from src.core.app_logger import get_logger
from src.models.database import get_session
from src.models.gift_card import GiftCard
from src.services.account_backend import get_account_backend
from src.services.member_service import SERVICE_NAME

logger = get_logger("gift_card")

# Crockford base32: digits + letters minus I, L, O (ambiguous) and U.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
_GROUP = 4  # display as ABCD-EFGH

# Typed lookalikes folded to their canonical character before matching.
_FOLD = str.maketrans({"I": "1", "L": "1", "O": "0", "U": "V"})

# Collisions are vanishingly unlikely (32^8 ≈ 1.1e12); this is just so a freak
# one retries instead of aborting a payment that already went through.
_MAX_CODE_ATTEMPTS = 5

# What callers outside the session get back. Deliberately not the ORM row: it
# would be detached the moment the session closes, and every caller here reads
# it after that point (the HTTP handler renders it, the callback returns it).
IssuedCard = namedtuple("IssuedCard", "code points redeemed")


def _snapshot(card):
    """Plain values captured while the row is still bound to its session."""
    if card is None:
        return None
    return IssuedCard(code=card.code, points=card.points,
                      redeemed=card.redeemed_at is not None)


# Redemption outcomes. `points` / `balance` are only meaningful when ok.
RedeemResult = namedtuple("RedeemResult", "status points balance code")

OK = "ok"
INVALID = "invalid"          # not a well-formed code, or no such card
ALREADY_USED = "already_used"
NO_MEMBER = "no_member"      # redeemer has no account yet (caller should create one)


def normalize_code(raw):
    """Fold user input to the stored form, or None if it can't be one.

    Forgiving on purpose: case, spaces, hyphens and the classic I/L/O
    mis-readings are all absorbed. Anything left that isn't a valid code is
    rejected rather than guessed at.
    """
    if not raw:
        return None
    cleaned = "".join(ch for ch in raw.upper() if ch.isalnum()).translate(_FOLD)
    if len(cleaned) != CODE_LENGTH:
        return None
    if any(ch not in _ALPHABET for ch in cleaned):
        return None
    return cleaned


def format_code(code):
    """ABCD-EFGH — grouped for reading aloud and typing without losing place."""
    if not code:
        return ""
    return "-".join(code[i:i + _GROUP] for i in range(0, len(code), _GROUP))


def _random_code():
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))


class GiftCardService:
    """Issues and redeems gift cards. The caller owns the session."""

    def __init__(self, backend=None):
        self._backend = backend or get_account_backend()

    # ----------------------------------------------------------------- issue

    def issue_for_order(self, session, order):
        """Mint the card for a paid gift order. Caller commits.

        Called from inside the payment callback's transaction, which already
        holds the order row lock and has checked that it was not settled
        before — so this does not re-check idempotency, it inherits it.
        """
        card = GiftCard(
            code=self._unused_code(session),
            order_id=order.id,
            points=order.points,
            status='active',
        )
        session.add(card)
        session.flush()
        logger.info(f"禮物卡已發出：{format_code(card.code)}（{card.points} 點，"
                    f"訂單 {order.merchant_trade_no}）")
        return _snapshot(card)

    def _unused_code(self, session):
        for _ in range(_MAX_CODE_ATTEMPTS):
            code = _random_code()
            taken = session.query(GiftCard.id).filter_by(code=code).first()
            if not taken:
                return code
            logger.warning(f"禮物卡代碼碰撞，重新產生：{code}")
        # The unique constraint is still the real guard; failing here means
        # something is badly wrong with the generator, not with luck.
        raise RuntimeError("無法產生未使用的禮物卡代碼")

    def card_for_order_no(self, session, merchant_trade_no):
        """The card issued for this order number, or None if not issued yet.

        The buyer's browser can land back on the result page before the
        provider's callback has arrived, so "not yet" is a normal answer here,
        not an error.
        """
        from src.models.payment_order import PaymentOrder  # local: avoid cycle

        card = (
            session.query(GiftCard)
            .join(PaymentOrder, PaymentOrder.id == GiftCard.order_id)
            .filter(PaymentOrder.merchant_trade_no == merchant_trade_no)
            .first()
        )
        return _snapshot(card)

    # ---------------------------------------------------------------- redeem

    def redeem_for_user(self, line_uid, raw_code):
        """Session-owning wrapper for callers that don't have one — i.e. the
        bot feature. Mirrors MemberService's style so features never open
        database sessions themselves."""
        with get_session() as session:
            try:
                return self.redeem(session, line_uid, raw_code)
            except Exception:
                session.rollback()
                logger.exception(f"兌換禮物卡失敗: {line_uid}")
                raise

    def redeem(self, session, line_uid, raw_code):
        """Credit a card's points to this LINE user, at most once ever.

        Returns a RedeemResult; the caller decides what to say. A malformed
        code and an unknown code are both INVALID on purpose — telling the
        difference would confirm which codes exist.
        """
        code = normalize_code(raw_code)
        if code is None:
            return RedeemResult(INVALID, 0, 0, None)

        # Lock the card before looking at redeemed_at: two people can type the
        # same code at the same moment.
        card = (
            session.query(GiftCard)
            .filter_by(code=code)
            .with_for_update()
            .first()
        )
        if card is None:
            return RedeemResult(INVALID, 0, 0, None)

        if card.redeemed_at is not None:
            logger.info(f"禮物卡重複兌換：{format_code(code)}")
            return RedeemResult(ALREADY_USED, card.points, 0, code)

        subject = self._backend.resolve(session, line_uid, for_update=True)
        if subject is None:
            session.rollback()
            logger.error(f"兌換禮物卡失敗：找不到會員 {line_uid}")
            return RedeemResult(NO_MEMBER, 0, 0, code)

        balance = self._backend.credit(
            session, subject, card.points,
            service=SERVICE_NAME,
            description=f"禮物卡兌換 {card.points} 點（{format_code(code)}）",
        )

        card.status = 'redeemed'
        card.redeemed_by_subject_id = subject.id
        card.redeemed_at = datetime.now(timezone.utc)

        session.commit()
        logger.info(f"禮物卡已兌換：{format_code(code)} +{card.points} 點，餘額 {balance}")
        return RedeemResult(OK, card.points, balance, code)
