"""Top-up orchestration: place an order, then credit points when — and only
when — the provider says it was paid.

Two rules shape everything here:

  1. **Only the server-to-server callback grants points.** The browser redirect
     after payment is user-controlled and proves nothing.
  2. **Crediting happens exactly once**, enforced by the database: a unique
     merchant_trade_no, a row lock on the order, and a `credited_at` stamp
     written in the same transaction as the ledger entry. In-memory dedup would
     not survive the app running multiple workers — the mistake already noted
     for webhook dedup in docs/HEALTH_CHECK.md.

The ledger write and the order update share one transaction on purpose: money
recorded as credited but points never granted (or the reverse) is the failure
mode this whole module exists to prevent.
"""
import os
import secrets
from collections import namedtuple
from datetime import datetime, timezone

from src.core.app_logger import get_logger
from src.core.settings import get_payment_settings
from src.models.payment_order import PaymentOrder
from src.services.account_backend import get_account_backend
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

CallbackResult = namedtuple("CallbackResult", "ok credited order reason")


class PaymentError(Exception):
    """Order could not be placed (unknown package, unknown member, no gateway)."""


def _new_merchant_trade_no(now):
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(_RANDOM_LEN))
    return f"{_PREFIX}{now.strftime('%y%m%d%H%M')}{suffix}"


class PaymentService:
    """Places orders and settles callbacks. The caller owns the session."""

    def __init__(self, ecpay=None, backend=None, settings_provider=get_payment_settings):
        self._ecpay = ecpay
        self._backend = backend or get_account_backend()
        self._settings_provider = settings_provider

    @property
    def enabled(self):
        """Top-up needs both a configured gateway and configured packages."""
        return self._ecpay is not None and self._settings_provider().enabled

    def packages(self):
        return self._settings_provider().packages

    def topup_link(self):
        """The LIFF page users pay on, or None when top-up can't be offered.

        Both halves have to be there: a configured gateway and a LIFF id to
        open the page with. Offering a link that leads nowhere is worse than
        not offering one, so this is the single check both the menu and the
        member feature ask.
        """
        liff_id = os.getenv("LIFF_ID")
        if not self.enabled or not liff_id:
            return None
        return f"https://liff.line.me/{liff_id}"

    # ------------------------------------------------------------------ order

    def create_order(self, session, line_uid, package_id, *,
                     return_url, order_result_url, client_back_url=None, now=None):
        """Create a pending order and return the form fields for ECPay.

        Points and price are snapshotted onto the order here; the callback later
        reads them from the row, never from config, so re-pricing cannot change
        what an in-flight order is worth.
        """
        if not self.enabled:
            raise PaymentError("儲值功能未啟用")

        package = self._settings_provider().package(package_id)
        if package is None:
            # Never fall back to a default package: the id comes from the client.
            raise PaymentError(f"沒有這個點數包：{package_id}")

        subject = self._backend.resolve(session, line_uid)
        if subject is None:
            raise PaymentError("找不到會員，請先加入好友")

        now = now or datetime.now(timezone.utc)
        order = PaymentOrder(
            subject_id=subject.id,
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
            amount=order.amount_twd,
            item_name=f"銀爺爺 {package.points} 點",
            trade_desc="銀爺爺點數儲值",
            return_url=return_url,
            order_result_url=order_result_url,
            client_back_url=client_back_url,
        )
        logger.info(f"建立儲值訂單 {order.merchant_trade_no}："
                    f"{order.points} 點 / NT${order.amount_twd}")
        return {'order': order, 'action': self._ecpay.api_url, 'params': params}

    # --------------------------------------------------------------- callback

    def handle_callback(self, session, payload):
        """Verify, then credit at most once. Returns a CallbackResult.

        `ok` is False only when the callback should be rejected outright (bad
        signature, unknown order, wrong amount). A payment that legitimately
        failed at the provider is `ok=True, credited=False` — it was handled,
        there is simply nothing to grant.
        """
        if not self._ecpay or not self._ecpay.verify(payload):
            return CallbackResult(False, False, None, "簽章驗證失敗")

        trade_no = payload.get('MerchantTradeNo')
        if not trade_no:
            return CallbackResult(False, False, None, "回調缺少 MerchantTradeNo")

        # Lock the order row: two workers can receive the same retry at once.
        order = (
            session.query(PaymentOrder)
            .filter_by(merchant_trade_no=trade_no)
            .with_for_update()
            .first()
        )
        if order is None:
            logger.warning(f"回調找不到對應訂單：{trade_no}")
            return CallbackResult(False, False, None, "找不到訂單")

        order.raw_callback = dict(payload)

        if str(payload.get('RtnCode')) != SUCCESS_RTN_CODE:
            # Provider says it did not succeed — record and stop.
            if order.credited_at is None:
                order.status = 'failed'
            session.commit()
            logger.info(f"訂單 {trade_no} 付款未成功：RtnCode={payload.get('RtnCode')}")
            return CallbackResult(True, False, order, "付款未成功")

        # A paid amount that disagrees with the order is either tampering or a
        # provider-side mismatch; either way, do not guess which one is right.
        if not self._amount_matches(payload, order):
            session.commit()
            logger.error(f"訂單 {trade_no} 金額不符：回調 {payload.get('TradeAmt')!r} "
                         f"≠ 訂單 {order.amount_twd}")
            return CallbackResult(False, False, order, "金額不符")

        if order.credited_at is not None:
            # ECPay retries until it gets 1|OK; this is the normal repeat path.
            session.commit()
            logger.info(f"訂單 {trade_no} 已入帳過，略過重複發點")
            return CallbackResult(True, False, order, "已入帳")

        subject = self._backend.get_by_id(session, order.subject_id, for_update=True)
        if subject is None:
            session.rollback()
            logger.error(f"訂單 {trade_no} 入帳失敗：找不到會員 {order.subject_id}")
            return CallbackResult(False, False, order, "找不到會員")

        balance = self._backend.credit(
            session, subject, order.points,
            service=SERVICE_NAME,
            description=f"儲值 {order.points} 點（訂單 {order.merchant_trade_no}）",
        )

        now = datetime.now(timezone.utc)
        order.status = 'paid'
        order.payment_type = payload.get('PaymentType')
        order.paid_at = order.paid_at or now
        order.credited_at = now

        session.commit()
        logger.info(f"訂單 {trade_no} 入帳完成：+{order.points} 點，餘額 {balance}")
        return CallbackResult(True, True, order, "入帳完成")

    @staticmethod
    def _amount_matches(payload, order):
        try:
            return int(payload.get('TradeAmt')) == order.amount_twd
        except (TypeError, ValueError):
            return False
