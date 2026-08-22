"""付款導回頁：綠界不論成功或失敗都把用戶導回這裡，所以頁面絕不能假設
「來到這頁＝付款成功」。

這條測試釘的就是那個曾經上線的 bug：刷卡被拒（RtnCode 付款失敗）時，頁面
照樣顯示大綠勾與「付款完成」，長輩以為成功、關掉頁面，再回頭發現沒點數。
成功頁誤報成功比任何事都糟——它把一次失敗變成一通客訴。
"""
import pytest

from src.app import app
import src.app as appmod


class FakeOrder:
    def __init__(self, status, points=400):
        self.status = status
        self.points = points


class FakeQuery:
    def __init__(self, order):
        self._order = order

    def filter_by(self, **kwargs):
        return self

    def first(self):
        return self._order


class FakeSession:
    def __init__(self, order):
        self._order = order

    def query(self, model):
        return FakeQuery(self._order)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def client():
    return app.test_client()


def wire_order(monkeypatch, order):
    """讓 route 內的 get_session() 交出一個帶指定訂單的假 session。"""
    monkeypatch.setattr(appmod, "get_session", lambda: FakeSession(order))


# ----------------------------------------------------------- /pay/done


class TestTopupResultPage:
    def test_paid_shows_success(self, client, monkeypatch):
        wire_order(monkeypatch, FakeOrder("paid"))

        html = client.get("/pay/done?no=GY123").data.decode()

        assert "付款完成" in html
        assert "400 點" in html

    def test_failed_never_says_completed(self, client, monkeypatch):
        wire_order(monkeypatch, FakeOrder("failed"))

        html = client.get("/pay/done?no=GY123").data.decode()

        assert "付款完成" not in html, (
            "刷卡失敗卻顯示「付款完成」——正是要修掉的謊報"
        )
        assert "沒有付款成功" in html
        assert "沒有向您收款" in html

    def test_pending_says_processing_not_completed(self, client, monkeypatch):
        wire_order(monkeypatch, FakeOrder("pending"))

        html = client.get("/pay/done?no=GY123").data.decode()

        assert "付款完成" not in html, (
            "導回頁比回調早到時狀態還是 pending，不能搶說完成"
        )
        assert "處理中" in html

    def test_unknown_order_does_not_claim_success(self, client, monkeypatch):
        wire_order(monkeypatch, None)

        html = client.get("/pay/done?no=GY404").data.decode()

        assert "付款完成" not in html
        assert "處理中" in html


# ----------------------------------------------------------- /gift/card


class TestGiftCardStatus:
    def _wire(self, monkeypatch, card, order):
        """gift/card 需要 gift_card_service 與（卡不存在時）訂單查詢。"""
        class FakeCardService:
            def card_for_order_no(self, session, order_no):
                return card

        monkeypatch.setattr(appmod, "gift_card_service", FakeCardService())
        monkeypatch.setattr(appmod, "_payments_ready", lambda: True)
        monkeypatch.setattr(appmod, "get_session", lambda: FakeSession(order))

    def test_failed_order_reports_failed_not_just_not_ready(self, client, monkeypatch):
        self._wire(monkeypatch, card=None, order=FakeOrder("failed"))

        data = client.get("/gift/card?no=GY123").get_json()

        assert data["ready"] is False
        assert data.get("failed") is True, (
            "付款失敗要讓前端停止空轉、直接請用戶重刷，"
            "而不是空轉 20 秒後顯示模糊的「還在處理中」"
        )

    def test_pending_order_is_not_ready_and_not_failed(self, client, monkeypatch):
        self._wire(monkeypatch, card=None, order=FakeOrder("pending"))

        data = client.get("/gift/card?no=GY123").get_json()

        assert data["ready"] is False
        assert "failed" not in data, "回調還沒到不是失敗，前端該繼續等"
