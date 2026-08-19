"""Top-up: signature verification, order placement, and credit-exactly-once.

Fully offline — no database, no ECPay. What is pinned here is the money path:
an unverified callback must never grant points, and a verified one must grant
them exactly once no matter how often ECPay retries.
"""
import pytest

from src.core.settings import SettingsError, _parse_payments
from src.models.payment_order import PaymentOrder
from src.services.ecpay_client import check_mac_value, verify_callback
from src.services.payment_service import PaymentError, PaymentService

HASH_KEY = "test-hash-key"
HASH_IV = "test-hash-iv"
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
    def __init__(self, orders=None):
        self.orders = list(orders or [])
        self.commits = 0
        self.rollbacks = 0

    def add(self, obj):
        self.orders.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def query(self, model):
        assert model is PaymentOrder, f"未預期的查詢對象: {model}"
        return FakeQuery(self.orders)


class FakeECPay:
    api_url = "https://payment-stage.example/AioCheckOut/V5"

    def __init__(self, valid=True):
        self.valid = valid
        self.last_params = None

    def checkout_params(self, **kwargs):
        self.last_params = kwargs
        return {"MerchantTradeNo": kwargs["merchant_trade_no"],
                "TotalAmount": str(kwargs["amount"]),
                "CheckMacValue": "FAKE"}

    def verify(self, payload):
        return self.valid


PACKAGES = {
    "provider": "ecpay",
    "packages": [
        {"id": "s", "points": 100, "price_twd": 100},
        {"id": "m", "points": 300, "price_twd": 250, "label": "300 點（較划算）"},
    ],
}


def build_service(*, subject=None, valid_signature=True, section=None):
    settings = _parse_payments(section if section is not None else PACKAGES)
    backend = FakeBackend(subject)
    ecpay = FakeECPay(valid=valid_signature)
    service = PaymentService(ecpay=ecpay, backend=backend,
                             settings_provider=lambda: settings)
    return service, backend, ecpay


def paid_callback(order, **overrides):
    payload = {
        "MerchantTradeNo": order.merchant_trade_no,
        "RtnCode": "1",
        "TradeAmt": str(order.amount_twd),
        "PaymentType": "Credit_CreditCard",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- settings


def test_no_payments_section_means_disabled():
    """既有部署的設定檔沒有 payments 區段，必須照常運作"""
    assert _parse_payments(None).enabled is False


def test_packages_parse():
    settings = _parse_payments(PACKAGES)
    assert settings.enabled
    assert settings.package("m").points == 300
    assert settings.package("s").label == "100 點"   # 未指定時自動生成


def test_unknown_package_is_none_not_a_default():
    """id 來自用戶端，找不到就必須是 None——退回預設會讓人用最低價買最多點"""
    assert _parse_payments(PACKAGES).package("nope") is None


@pytest.mark.parametrize("section,reason", [
    ({"provider": "paypal", "packages": [{"id": "s", "points": 1, "price_twd": 1}]}, "不支援的金流"),
    ({"provider": "ecpay", "packages": []}, "空清單"),
    ({"provider": "ecpay", "packages": [{"id": "s", "points": 0, "price_twd": 1}]}, "點數為 0"),
    ({"provider": "ecpay", "packages": [{"id": "s", "points": 1, "price_twd": -5}]}, "負數金額"),
    ({"provider": "ecpay", "packages": [{"id": "a b", "points": 1, "price_twd": 1}]}, "id 含空白"),
    ({"provider": "ecpay", "packages": [{"id": "s", "points": 1, "price_twd": 1},
                                        {"id": "s", "points": 2, "price_twd": 2}]}, "id 重複"),
])
def test_bad_payments_config_refuses_to_load(section, reason):
    """設定寫錯要讓部署中止，而不是靜悄悄關掉金流"""
    with pytest.raises(SettingsError):
        _parse_payments(section)


# ---------------------------------------------------------------- signature


def test_signature_is_order_independent():
    a = {"MerchantID": "2000132", "TotalAmount": "100", "MerchantTradeNo": "GY1"}
    b = {"MerchantTradeNo": "GY1", "MerchantID": "2000132", "TotalAmount": "100"}
    assert check_mac_value(a, HASH_KEY, HASH_IV) == check_mac_value(b, HASH_KEY, HASH_IV)


def test_signature_excludes_the_signature_field():
    params = {"MerchantTradeNo": "GY1", "TotalAmount": "100"}
    expected = check_mac_value(params, HASH_KEY, HASH_IV)
    assert check_mac_value({**params, "CheckMacValue": "WHATEVER"}, HASH_KEY, HASH_IV) == expected


def test_verify_accepts_own_signature():
    params = {"MerchantTradeNo": "GY1", "TotalAmount": "100"}
    params["CheckMacValue"] = check_mac_value(params, HASH_KEY, HASH_IV)
    assert verify_callback(params, HASH_KEY, HASH_IV)


def test_verify_is_case_insensitive_about_the_mac():
    params = {"MerchantTradeNo": "GY1", "TotalAmount": "100"}
    params["CheckMacValue"] = check_mac_value(params, HASH_KEY, HASH_IV).lower()
    assert verify_callback(params, HASH_KEY, HASH_IV)


def test_verify_rejects_tampered_amount():
    params = {"MerchantTradeNo": "GY1", "TotalAmount": "100"}
    params["CheckMacValue"] = check_mac_value(params, HASH_KEY, HASH_IV)
    params["TotalAmount"] = "10000"
    assert not verify_callback(params, HASH_KEY, HASH_IV)


def test_verify_rejects_missing_mac():
    assert not verify_callback({"MerchantTradeNo": "GY1"}, HASH_KEY, HASH_IV)


def test_verify_rejects_wrong_key():
    params = {"MerchantTradeNo": "GY1"}
    params["CheckMacValue"] = check_mac_value(params, HASH_KEY, HASH_IV)
    assert not verify_callback(params, "someone-elses-key", HASH_IV)


# ---------------------------------------------------------------- order


def test_create_order_snapshots_price_and_points():
    """訂單鎖住當下的點數與金額，日後調價不影響已開立的單"""
    service, _, ecpay = build_service(subject=FakeSubject())
    session = FakeSession()

    result = service.create_order(session, UID, "m",
                                  return_url="https://x.test/cb",
                                  order_result_url="https://x.test/done")
    order = result["order"]
    assert (order.points, order.amount_twd, order.status) == (300, 250, "pending")
    assert order.credited_at is None
    assert len(order.merchant_trade_no) <= 20 and order.merchant_trade_no.isalnum()
    assert result["action"] == ecpay.api_url


def test_create_order_rejects_unknown_package():
    service, _, _ = build_service(subject=FakeSubject())
    with pytest.raises(PaymentError):
        service.create_order(FakeSession(), UID, "does-not-exist",
                             return_url="https://x.test/cb",
                             order_result_url="https://x.test/done")


def test_create_order_rejects_unknown_member():
    service, _, _ = build_service(subject=None)
    with pytest.raises(PaymentError):
        service.create_order(FakeSession(), UID, "s",
                             return_url="https://x.test/cb",
                             order_result_url="https://x.test/done")


def test_create_order_disabled_without_gateway():
    settings = _parse_payments(PACKAGES)
    service = PaymentService(ecpay=None, backend=FakeBackend(FakeSubject()),
                             settings_provider=lambda: settings)
    assert service.enabled is False
    with pytest.raises(PaymentError):
        service.create_order(FakeSession(), UID, "s",
                             return_url="https://x.test/cb",
                             order_result_url="https://x.test/done")


# ---------------------------------------------------------------- callback


def placed_order(service, session, subject, package="s"):
    return service.create_order(session, UID, package,
                                return_url="https://x.test/cb",
                                order_result_url="https://x.test/done")["order"]


def test_paid_callback_credits_points():
    subject = FakeSubject(points_balance=20)
    service, backend, _ = build_service(subject=subject)
    session = FakeSession()
    order = placed_order(service, session, subject)

    result = service.handle_callback(session, paid_callback(order))

    assert (result.ok, result.credited) == (True, True)
    assert subject.points_balance == 120
    assert order.status == "paid"
    assert order.credited_at is not None
    assert order.payment_type == "Credit_CreditCard"
    assert len(backend.ledger) == 1
    assert order.merchant_trade_no in backend.ledger[0][2], "帳本要留下訂單編號供對帳"


def test_retried_callback_credits_only_once():
    """綠界會重送到收到 1|OK 為止——重送不可以再發一次點"""
    subject = FakeSubject(points_balance=0)
    service, backend, _ = build_service(subject=subject)
    session = FakeSession()
    order = placed_order(service, session, subject)
    payload = paid_callback(order)

    first = service.handle_callback(session, payload)
    second = service.handle_callback(session, payload)
    third = service.handle_callback(session, payload)

    assert first.credited is True
    assert (second.credited, third.credited) == (False, False)
    # 重送仍要回報成功，否則綠界會無限重試
    assert (second.ok, third.ok) == (True, True)
    assert subject.points_balance == 100
    assert len(backend.ledger) == 1


def test_bad_signature_credits_nothing():
    subject = FakeSubject(points_balance=0)
    service, backend, _ = build_service(subject=subject, valid_signature=False)
    session = FakeSession()
    order = PaymentOrder(subject_id=subject.id, merchant_trade_no="GY0001",
                         package_id="s", points=100, amount_twd=100, status="pending")
    session.add(order)

    result = service.handle_callback(session, paid_callback(order))

    assert (result.ok, result.credited) == (False, False)
    assert subject.points_balance == 0
    assert backend.ledger == []


def test_amount_mismatch_credits_nothing():
    """回調金額與訂單不符：可能是竄改，不猜哪邊才對"""
    subject = FakeSubject(points_balance=0)
    service, backend, _ = build_service(subject=subject)
    session = FakeSession()
    order = placed_order(service, session, subject)

    result = service.handle_callback(session, paid_callback(order, TradeAmt="1"))

    assert (result.ok, result.credited) == (False, False)
    assert subject.points_balance == 0
    assert order.credited_at is None
    assert backend.ledger == []


@pytest.mark.parametrize("amount", [None, "", "abc"])
def test_unparseable_amount_credits_nothing(amount):
    subject = FakeSubject(points_balance=0)
    service, backend, _ = build_service(subject=subject)
    session = FakeSession()
    order = placed_order(service, session, subject)

    result = service.handle_callback(session, paid_callback(order, TradeAmt=amount))

    assert result.credited is False
    assert backend.ledger == []


def test_failed_payment_is_handled_but_not_credited():
    subject = FakeSubject(points_balance=0)
    service, backend, _ = build_service(subject=subject)
    session = FakeSession()
    order = placed_order(service, session, subject)

    result = service.handle_callback(session, paid_callback(order, RtnCode="10100248"))

    assert (result.ok, result.credited) == (True, False)
    assert order.status == "failed"
    assert subject.points_balance == 0
    assert backend.ledger == []


def test_unknown_order_is_rejected():
    service, backend, _ = build_service(subject=FakeSubject())
    result = service.handle_callback(FakeSession(), {
        "MerchantTradeNo": "GY-NOT-MINE", "RtnCode": "1", "TradeAmt": "100"})
    assert (result.ok, result.credited) == (False, False)
    assert backend.ledger == []


def test_callback_without_order_number_is_rejected():
    service, _, _ = build_service(subject=FakeSubject())
    result = service.handle_callback(FakeSession(), {"RtnCode": "1", "TradeAmt": "100"})
    assert result.ok is False


def test_callback_keeps_raw_payload_for_disputes():
    subject = FakeSubject()
    service, _, _ = build_service(subject=subject)
    session = FakeSession()
    order = placed_order(service, session, subject)

    payload = paid_callback(order, SimulatePaid="0")
    service.handle_callback(session, payload)

    assert order.raw_callback == payload
