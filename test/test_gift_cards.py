"""Gift cards: issue exactly one per paid order, redeem it exactly once.

Fully offline — no database, no ECPay. What is pinned here is the same money
path test_payment.py pins for top-ups, plus the two things unique to gifts:
a card is a *bearer* instrument (whoever types the code gets the points), and
it can be spent only once no matter how many people type it.

The code-handling tests are not incidental: the person typing a code is often
80 years old, reading it off a screenshot their grandchild sent, so tolerating
case, hyphens and the classic I/L/O mis-readings is a product requirement.
"""
import pytest

from src.core.settings import _parse_payments
from src.models.gift_card import GiftCard
from src.services import gift_card_service as gc
from src.services.gift_card_service import GiftCardService
from src.services.payment_service import KIND_GIFT, PaymentService

UID = "U" + "a" * 32


# ---------------------------------------------------------------- fakes


class FakeSubject:
    def __init__(self, subject_id="acc-1", points_balance=0):
        self.id = subject_id
        self.points_balance = points_balance


class FakeBackend:
    def __init__(self, subject=None):
        self.subject = subject
        self.ledger = []

    def resolve(self, session, line_uid, *, for_update=False):
        return self.subject

    def get_by_id(self, session, subject_id, *, for_update=False):
        if self.subject is not None and self.subject.id == subject_id:
            return self.subject
        return None

    def credit(self, session, subject, points, *, service, description):
        subject.points_balance += points
        self.ledger.append((subject.id, points, description))
        return subject.points_balance

    def provider_uid_map(self, session, subject_ids):
        if self.subject is not None and self.subject.id in subject_ids:
            return {self.subject.id: UID}
        return {}


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter_by(self, **kwargs):
        return FakeQuery([r for r in self._rows
                          if all(getattr(r, k) == v for k, v in kwargs.items())])

    def with_for_update(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Holds gift cards only; the payment side is faked separately."""

    def __init__(self, cards=None):
        self.cards = list(cards or [])
        self.commits = 0
        self.rollbacks = 0

    def add(self, obj):
        self.cards.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def query(self, model):
        # `is`, not `in`: comparing a mapped column with == builds a SQL
        # expression instead of answering a Python question.
        assert model is GiftCard or model is GiftCard.id, f"未預期的查詢對象: {model}"
        return FakeQuery(self.cards)


class FakeOrder:
    def __init__(self, order_id="order-1", points=300, kind=KIND_GIFT,
                 trade_no="GY2608191200ABCDEFGH", subject_id=None):
        self.id = order_id
        self.points = points
        self.kind = kind
        self.merchant_trade_no = trade_no
        self.subject_id = subject_id
        self.status = 'pending'
        self.credited_at = None
        self.paid_at = None
        self.payment_type = None
        self.raw_callback = None
        self.amount_twd = 250


def issued_card(code="ABCD1234", points=300, redeemed=False):
    card = GiftCard(code=code, order_id="order-1", points=points,
                    status='redeemed' if redeemed else 'active')
    if redeemed:
        from datetime import datetime, timezone
        card.redeemed_at = datetime.now(timezone.utc)
    return card


# ------------------------------------------------------------ code handling


@pytest.mark.parametrize("typed", [
    "ABCD1234",
    "abcd1234",
    "ABCD-1234",
    "abcd-1234",
    "ABCD 1234",
    " ABCD-1234 ",
])
def test_normalize_absorbs_how_people_actually_type_it(typed):
    assert gc.normalize_code(typed) == "ABCD1234"


def test_normalize_folds_the_classic_misreadings():
    """卡號沒有 I/L/O，所以打成 I/L/O 一定是看錯了 1 或 0——直接接受"""
    assert gc.normalize_code("O1I2L345") == "01121345"[:8]


@pytest.mark.parametrize("bad", ["", None, "ABC123", "ABCD12345", "ABCD-12!4", "中文中文中文中文"])
def test_normalize_rejects_what_cannot_be_a_code(bad):
    assert gc.normalize_code(bad) is None


def test_format_code_groups_for_reading_aloud():
    assert gc.format_code("ABCD1234") == "ABCD-1234"


def test_generated_codes_avoid_ambiguous_characters():
    service = GiftCardService(backend=FakeBackend())
    for _ in range(50):
        code = service._unused_code(FakeSession())
        assert len(code) == gc.CODE_LENGTH
        assert not set(code) & set("ILOU"), f"{code} 含有容易看錯的字元"
        # A generated code must survive its own normalisation
        assert gc.normalize_code(gc.format_code(code)) == code


def test_generation_retries_past_a_collision(monkeypatch):
    taken = issued_card(code="AAAA1111")
    codes = iter(["AAAA1111", "BBBB2222"])
    monkeypatch.setattr(gc, "_random_code", lambda: next(codes))

    service = GiftCardService(backend=FakeBackend())
    assert service._unused_code(FakeSession([taken])) == "BBBB2222"


# ------------------------------------------------------------------ issuing


def build_payment_service(gift_cards, subject=None):
    settings = _parse_payments({
        "provider": "ecpay",
        "packages": [{"id": "m", "points": 300, "price_twd": 250}],
    })

    class FakeECPay:
        api_url = "https://payment-stage.example/AioCheckOut/V5"

        def checkout_params(self, **kwargs):
            return {"MerchantTradeNo": kwargs["merchant_trade_no"]}

        def verify(self, payload):
            return True

    backend = FakeBackend(subject)
    service = PaymentService(ecpay=FakeECPay(), backend=backend,
                             settings_provider=lambda: settings,
                             gift_cards=gift_cards)
    return service, backend


class RecordingGiftCards:
    def __init__(self):
        self.issued = []

    def issue_for_order(self, session, order):
        # Mirrors the real service: a snapshot, not the ORM row
        card = gc.IssuedCard(code="ZZZZ9999", points=order.points, redeemed=False, sent=False)
        self.issued.append(card)
        return card

    def card_for_order_no(self, session, merchant_trade_no):
        return self.issued[0] if self.issued else None


class OrderSession(FakeSession):
    """A session whose PaymentOrder lookup returns a single known order."""

    def __init__(self, order):
        super().__init__()
        self.order = order

    def query(self, model):
        return FakeQuery([self.order])


def paid_callback(order, **overrides):
    payload = {
        "MerchantTradeNo": order.merchant_trade_no,
        "RtnCode": "1",
        "TradeAmt": str(order.amount_twd),
        "PaymentType": "Credit_CreditCard",
    }
    payload.update(overrides)
    return payload


def test_paid_gift_order_issues_a_card_and_credits_nobody():
    """禮物單付款成功發的是卡，不是點——收禮的人這時候還不存在"""
    cards = RecordingGiftCards()
    service, backend = build_payment_service(cards)
    order = FakeOrder()

    result = service.handle_callback(OrderSession(order), paid_callback(order))

    assert result.ok and result.credited
    assert result.card.points == 300
    assert backend.ledger == [], "禮物單不該當場給任何人加點"
    assert order.status == 'paid' and order.credited_at is not None


def test_retried_callback_does_not_issue_a_second_card():
    """綠界會重送到收到 1|OK 為止；重送不能變成多印一張卡"""
    cards = RecordingGiftCards()
    service, _ = build_payment_service(cards)
    order = FakeOrder()
    session = OrderSession(order)

    first = service.handle_callback(session, paid_callback(order))
    second = service.handle_callback(session, paid_callback(order))

    assert first.credited is True
    assert second.ok is True and second.credited is False
    assert len(cards.issued) == 1
    # 重送時仍然回得出同一張卡，導回頁才查得到
    assert second.card is cards.issued[0]


def test_failed_gift_payment_issues_nothing():
    cards = RecordingGiftCards()
    service, _ = build_payment_service(cards)
    order = FakeOrder()

    result = service.handle_callback(OrderSession(order),
                                     paid_callback(order, RtnCode="10100248"))

    assert result.ok and not result.credited
    assert cards.issued == []
    assert order.status == 'failed'


def test_tampered_amount_issues_nothing():
    cards = RecordingGiftCards()
    service, _ = build_payment_service(cards)
    order = FakeOrder()

    result = service.handle_callback(OrderSession(order),
                                     paid_callback(order, TradeAmt="1"))

    assert result.ok is False
    assert cards.issued == []


def test_gift_order_needs_no_member():
    """買的人不必是會員，也不必登入 LINE——這正是禮物卡的重點"""
    cards = RecordingGiftCards()
    service, _ = build_payment_service(cards, subject=None)

    result = service.create_order(FakeSession(), None, "m", kind=KIND_GIFT,
                                  return_url="https://x.test/cb",
                                  order_result_url="https://x.test/done")

    order = result["order"]
    assert order.kind == KIND_GIFT
    assert order.subject_id is None
    assert order.points == 300


def test_topup_order_still_requires_a_member():
    """一般儲值沒有收點的人就不能成立（回歸）"""
    from src.services.payment_service import PaymentError

    cards = RecordingGiftCards()
    service, _ = build_payment_service(cards, subject=None)

    with pytest.raises(PaymentError):
        service.create_order(FakeSession(), UID, "m",
                             return_url="https://x.test/cb",
                             order_result_url="https://x.test/done")


# ----------------------------------------------------------------- redeeming


_DEFAULT = object()


def build_gift_service(subject=_DEFAULT):
    """subject=None means "this LINE user has no account" — a real case the
    service must handle, so it cannot double as "use the default"."""
    backend = FakeBackend(FakeSubject() if subject is _DEFAULT else subject)
    return GiftCardService(backend=backend), backend


def test_redeem_credits_the_points_once():
    card = issued_card(points=300)
    service, backend = build_gift_service()
    session = FakeSession([card])

    result = service.redeem(session, UID, "ABCD-1234")

    assert result.status == gc.OK
    assert result.points == 300 and result.balance == 300
    assert card.status == 'redeemed' and card.redeemed_at is not None
    assert card.redeemed_by_subject_id == "acc-1"
    assert len(backend.ledger) == 1


def test_second_redeem_of_the_same_card_credits_nothing():
    card = issued_card(points=300)
    service, backend = build_gift_service()
    session = FakeSession([card])

    service.redeem(session, UID, "ABCD1234")
    result = service.redeem(session, UID, "ABCD1234")

    assert result.status == gc.ALREADY_USED
    assert len(backend.ledger) == 1, "第二次兌換不能再加點"


def test_redeem_accepts_the_hyphenated_lowercase_form():
    card = issued_card(points=100)
    service, backend = build_gift_service()

    result = service.redeem(FakeSession([card]), UID, "  abcd-1234 ")

    assert result.status == gc.OK
    assert backend.ledger[0][1] == 100


def test_unknown_code_credits_nothing():
    service, backend = build_gift_service()

    result = service.redeem(FakeSession([issued_card()]), UID, "ZZZZ9999")

    assert result.status == gc.INVALID
    assert backend.ledger == []


def test_malformed_code_is_rejected_without_touching_the_card_table():
    service, backend = build_gift_service()
    session = FakeSession([issued_card()])

    result = service.redeem(session, UID, "不是卡號")

    assert result.status == gc.INVALID
    assert backend.ledger == [] and session.commits == 0


def test_redeem_without_an_account_credits_nothing():
    """理論上不會發生（功能層會先建會員），但不能默默吞掉一張卡"""
    service, backend = build_gift_service(subject=None)
    card = issued_card()

    result = service.redeem(FakeSession([card]), UID, "ABCD1234")

    assert result.status == gc.NO_MEMBER
    assert card.redeemed_at is None, "沒加到點的卡不能被標記為已兌換"
    assert backend.ledger == []


# ------------------------------------------ 記住買家：付完款關頁也救得回

def test_gift_order_records_the_buyer_when_identified():
    """從 LINE 內買（有身分）時記下買家，方便事後提醒他送出禮物"""
    buyer = FakeSubject(subject_id="buyer-1")
    cards = RecordingGiftCards()
    service, _ = build_payment_service(cards, subject=buyer)

    result = service.create_order(FakeSession(), UID, "m", kind=KIND_GIFT,
                                  return_url="https://x.test/cb",
                                  order_result_url="https://x.test/done")

    assert result["order"].subject_id == "buyer-1", "應記下買家 subject"


def test_anonymous_gift_order_records_no_buyer():
    """一般網頁買家（沒身分）維持不記名"""
    cards = RecordingGiftCards()
    service, _ = build_payment_service(cards, subject=None)

    result = service.create_order(FakeSession(), None, "m", kind=KIND_GIFT,
                                  return_url="https://x.test/cb",
                                  order_result_url="https://x.test/done")

    assert result["order"].subject_id is None


def test_paid_gift_returns_buyer_uid_for_the_nudge():
    """付款成功、且記了買家 → 回傳買家 line_uid，讓上層推「快送出」提醒"""
    buyer = FakeSubject(subject_id="buyer-1")
    cards = RecordingGiftCards()
    service, _ = build_payment_service(cards, subject=buyer)
    order = FakeOrder(subject_id="buyer-1")

    result = service.handle_callback(OrderSession(order), paid_callback(order))

    assert result.credited and result.buyer_uid == UID


def test_paid_anonymous_gift_returns_no_buyer_uid():
    """不記名的禮物單付款成功，沒有可提醒的對象"""
    cards = RecordingGiftCards()
    service, _ = build_payment_service(cards, subject=None)
    order = FakeOrder(subject_id=None)

    result = service.handle_callback(OrderSession(order), paid_callback(order))

    assert result.credited and result.buyer_uid is None
