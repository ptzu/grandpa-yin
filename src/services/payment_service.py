"""Order orchestration: place an order, then settle it when — and only when —
the provider says it was paid.

Two rules shape everything here:

  1. **Only the server-to-server callback settles an order.** The browser
     redirect after payment is user-controlled and proves nothing.
  2. **Settlement happens exactly once**, enforced by the database: a unique
     merchant_trade_no, a row lock on the order, and a `credited_at` stamp
     written in the same transaction as whatever the settlement produced.
     In-memory dedup would not survive the app running multiple workers — the
     mistake already noted for webhook dedup in docs/HEALTH_CHECK.md.

Two kinds of order settle differently and share everything else:

  * `topup` — points land in the buyer's own balance.
  * `gift`  — a gift card is issued for someone else to redeem later, so the
    order has no subject at all until that happens.

The settlement write and the order update share one transaction on purpose:
money recorded as settled but nothing actually granted (or the reverse) is the
failure mode this whole module exists to prevent.
"""
import os
import secrets
from collections import namedtuple
from datetime import datetime, timezone

from src.core.app_logger import get_logger
from src.core.settings import get_payment_settings
from src.models.payment_order import PaymentOrder
from src.services.account_backend import get_account_backend
from src.services.gift_card_service import GiftCardService
from src.services.member_service import SERVICE_NAME

logger = get_logger("payment")

# ECPay allows 20 alphanumeric chars; "GY" + yymmddHHMM + 8 random = 20.
_PREFIX = "GY"
_RANDOM_LEN = 8
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

SUCCESS_RTN_CODE = "1"

# What the caller should write back to ECPay. Anything other than "1|OK" makes
# it retry, so an already-processed callback still answers OK.
ECPAY_ACK = "1|OK"
ECPAY_REJECT = "0|error"

# `credited` means "this callback settled the order" — points granted for a
# top-up, or a card issued for a gift. `card` is set only for a gift; `buyer_uid`
# is the gift buyer's LINE id when known, so the caller can nudge them to send.
CallbackResult = namedtuple("CallbackResult", "ok credited order reason card buyer_uid")


def _result(ok, credited, order, reason, card=None, buyer_uid=None):
    return CallbackResult(ok, credited, order, reason, card, buyer_uid)


KIND_TOPUP = "topup"
KIND_GIFT = "gift"

# LIFF_ENDPOINT_NOTE
# 這個服務有兩頁需要在 LINE 裡開：付款與「選家人送禮物卡」。一個 LIFF app 只能
# 設一個 Endpoint URL，所以 Console 的 Endpoint URL 填**服務根網址**
# （https://<app>/），要開哪一頁由 query 參數決定：
#     https://liff.line.me/<LIFF_ID>?p=start           ← bot 給的唯一連結
#     https://liff.line.me/<LIFF_ID>?p=pay
#     https://liff.line.me/<LIFF_ID>?p=share&no=<訂單編號>
#
# 不用 `liff.line.me/<id>/<path>` 的路徑寫法：實測那條會落回 Endpoint URL 本身，
# 而且 LINE 會把結果當成「外部網站」而不是 LIFF app，shareTargetPicker 這類
# API 就全部不可用。根路由也刻意不做 302 轉址，理由相同。


class PaymentError(Exception):
    """Order could not be placed (unknown package, unknown member, no gateway)."""


def _new_merchant_trade_no(now):
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(_RANDOM_LEN))
    return f"{_PREFIX}{now.strftime('%y%m%d%H%M')}{suffix}"


class PaymentService:
    """Places orders and settles callbacks. The caller owns the session."""

    def __init__(self, ecpay=None, backend=None, settings_provider=get_payment_settings,
                 gift_cards=None):
        self._ecpay = ecpay
        self._backend = backend or get_account_backend()
        self._settings_provider = settings_provider
        self._gift_cards = gift_cards or GiftCardService(backend=self._backend)

    @property
    def enabled(self):
        """Top-up needs both a configured gateway and configured packages."""
        return self._ecpay is not None and self._settings_provider().enabled

    def packages(self):
        return self._settings_provider().packages

    def topup_link(self):
        """The one link the bot hands out for buying points, or None.

        Points to the chooser (buy for myself / buy as a gift), not straight to
        the payment page: two links in one message is one too many for the
        audience, and "who is this for?" is a question they can answer.

        Both halves have to be there: a configured gateway and a LIFF id to
        open the page with. Offering a link that leads nowhere is worse than
        not offering one, so this is the single check both the menu and the
        member feature ask.

        Which page to open travels as a query parameter, not as a path on the
        LIFF URL: one LIFF app has one endpoint and this service has two pages
        that need it — see LIFF_ENDPOINT_NOTE below for why the path form does
        not work.
        """
        liff_id = os.getenv("LIFF_ID")
        if not self.enabled or not liff_id:
            return None
        return f"https://liff.line.me/{liff_id}?p=start"

    def gift_link(self):
        """The open-web page where someone buys a card for someone else.

        Deliberately not a LIFF page: the buyer is usually the adult child, who
        may be on a laptop and may not even be a LINE user. A bearer card needs
        no identity at purchase, so requiring a LINE login would only add a
        place to give up.

        Needs PUBLIC_BASE_URL because the bot builds this link outside any HTTP
        request. Unset means the bot simply never mentions gifting — same rule
        as top-up: a link that leads nowhere is worse than no link.
        """
        base = os.getenv("PUBLIC_BASE_URL")
        if not self.enabled or not base:
            return None
        return f"{base.rstrip('/')}/gift"

    # ------------------------------------------------------------------ order

    def create_order(self, session, line_uid, package_id, *, kind=KIND_TOPUP,
                     return_url, order_result_url, client_back_url=None, now=None):
        """Create a pending order and return the form fields for ECPay.

        Points and price are snapshotted onto the order here; the callback later
        reads them from the row, never from config, so re-pricing cannot change
        what an in-flight order is worth.

        A `gift` order takes no line_uid: nobody is credited until a card is
        redeemed. A `topup` order must resolve to an existing member, because
        the points have nowhere else to go.
        """
        if not self.enabled:
            raise PaymentError("儲值功能未啟用")

        if kind not in (KIND_TOPUP, KIND_GIFT):
            raise PaymentError(f"未知的訂單類型：{kind}")

        package = self._settings_provider().package(package_id)
        if package is None:
            # Never fall back to a default package: the id comes from the client.
            raise PaymentError(f"沒有這個點數包：{package_id}")

        subject_id = None
        if kind == KIND_TOPUP:
            subject = self._backend.resolve(session, line_uid)
            if subject is None:
                raise PaymentError("找不到會員，請先加入好友")
            subject_id = subject.id
        elif line_uid:
            # Gift buyer is optional but useful: knowing who paid lets the bot
            # nudge them to actually send the card if they close the page before
            # picking a friend. Not found → stay anonymous, no nudge.
            buyer = self._backend.resolve(session, line_uid)
            if buyer is not None:
                subject_id = buyer.id

        is_gift = kind == KIND_GIFT
        now = now or datetime.now(timezone.utc)
        order = PaymentOrder(
            subject_id=subject_id,
            kind=kind,
            merchant_trade_no=_new_merchant_trade_no(now),
            package_id=package.id,
            points=package.points,
            amount_twd=package.price_twd,
            status='pending',
        )
        session.add(order)
        session.flush()

        params = self._ecpay.checkout_params(
            merchant_trade_no=order.merchant_trade_no,
            trade_date=now.strftime("%Y/%m/%d %H:%M:%S"),
            item_name=(f"銀爺爺點數禮物卡 {package.points} 點" if is_gift
                       else f"銀爺爺 {package.points} 點"),
            amount=order.amount_twd,
            trade_desc="銀爺爺點數禮物卡" if is_gift else "銀爺爺點數儲值",
            return_url=return_url,
            order_result_url=order_result_url,
            client_back_url=client_back_url,
        )
        logger.info(f"建立{'禮物卡' if is_gift else '儲值'}訂單 {order.merchant_trade_no}："
                    f"{order.points} 點 / NT${order.amount_twd}")
        return {'order': order, 'action': self._ecpay.api_url, 'params': params}

    # --------------------------------------------------------------- callback

    def handle_callback(self, session, payload):
        """Verify, then settle at most once. Returns a CallbackResult.

        `ok` is False only when the callback should be rejected outright (bad
        signature, unknown order, wrong amount). A payment that legitimately
        failed at the provider is `ok=True, credited=False` — it was handled,
        there is simply nothing to grant.
        """
        if not self._ecpay or not self._ecpay.verify(payload):
            return _result(False, False, None, "簽章驗證失敗")

        trade_no = payload.get('MerchantTradeNo')
        if not trade_no:
            return _result(False, False, None, "回調缺少 MerchantTradeNo")

        # Lock the order row: two workers can receive the same retry at once.
        order = (
            session.query(PaymentOrder)
            .filter_by(merchant_trade_no=trade_no)
            .with_for_update()
            .first()
        )
        if order is None:
            logger.warning(f"回調找不到對應訂單：{trade_no}")
            return _result(False, False, None, "找不到訂單")

        order.raw_callback = dict(payload)

        if str(payload.get('RtnCode')) != SUCCESS_RTN_CODE:
            # Provider says it did not succeed — record and stop.
            if order.credited_at is None:
                order.status = 'failed'
            session.commit()
            logger.info(f"訂單 {trade_no} 付款未成功：RtnCode={payload.get('RtnCode')}")
            return _result(True, False, order, "付款未成功")

        # A paid amount that disagrees with the order is either tampering or a
        # provider-side mismatch; either way, do not guess which one is right.
        if not self._amount_matches(payload, order):
            session.commit()
            logger.error(f"訂單 {trade_no} 金額不符：回調 {payload.get('TradeAmt')!r} "
                         f"≠ 訂單 {order.amount_twd}")
            return _result(False, False, order, "金額不符")

        if order.credited_at is not None:
            # ECPay retries until it gets 1|OK; this is the normal repeat path.
            session.commit()
            logger.info(f"訂單 {trade_no} 已入帳過，略過重複處理")
            return _result(True, False, order, "已入帳",
                           card=self._issued_card(session, order))

        # Everything above is common to both kinds; only the settlement below
        # differs, and it happens under the same lock and the same commit.
        if order.kind == KIND_GIFT:
            return self._settle_gift(session, order, payload)
        return self._settle_topup(session, order, payload)

    def _settle_topup(self, session, order, payload):
        """Points into the buyer's own balance."""
        subject = self._backend.get_by_id(session, order.subject_id, for_update=True)
        if subject is None:
            session.rollback()
            logger.error(f"訂單 {order.merchant_trade_no} 入帳失敗："
                         f"找不到會員 {order.subject_id}")
            return _result(False, False, order, "找不到會員")

        balance = self._backend.credit(
            session, subject, order.points,
            service=SERVICE_NAME,
            description=f"儲值 {order.points} 點（訂單 {order.merchant_trade_no}）",
        )
        self._stamp_settled(order, payload)

        session.commit()
        logger.info(f"訂單 {order.merchant_trade_no} 入帳完成："
                    f"+{order.points} 點，餘額 {balance}")
        return _result(True, True, order, "入帳完成")

    def _settle_gift(self, session, order, payload):
        """A card instead of points — nobody is credited until it is redeemed."""
        card = self._gift_cards.issue_for_order(session, order)
        self._stamp_settled(order, payload)
        buyer_uid = self._buyer_uid(session, order)

        session.commit()
        logger.info(f"訂單 {order.merchant_trade_no} 已開立禮物卡（{order.points} 點）")
        return _result(True, True, order, "已開立禮物卡", card=card, buyer_uid=buyer_uid)

    def _buyer_uid(self, session, order):
        """The gift buyer's LINE id, or None when the order was anonymous."""
        if not order.subject_id:
            return None
        return self._backend.provider_uid_map(
            session, [order.subject_id]).get(order.subject_id)

    @staticmethod
    def _stamp_settled(order, payload):
        now = datetime.now(timezone.utc)
        order.status = 'paid'
        order.payment_type = payload.get('PaymentType')
        order.paid_at = order.paid_at or now
        order.credited_at = now

    def _issued_card(self, session, order):
        """The card this order already produced, for repeat callbacks."""
        if order.kind != KIND_GIFT:
            return None
        return self._gift_cards.card_for_order_no(session, order.merchant_trade_no)

    @staticmethod
    def _amount_matches(payload, order):
        try:
            return int(payload.get('TradeAmt')) == order.amount_twd
        except (TypeError, ValueError):
            return False
