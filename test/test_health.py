"""/health：讓監控在用戶之前先發現服務掛了。

這條路徑存在的唯一理由是「回 200 就代表真的能服務一則訊息」。所以測試盯的
不是它會不會回應，而是它在該說不健康的時候有沒有說——只驗證 HTTP 活著的
健康檢查，在資料庫掛掉時照樣回 200，等於沒有監控。
"""
import json

import pytest

from src.app import app


@pytest.fixture
def client():
    return app.test_client()


def set_state(monkeypatch, initialized=True, database=True):
    monkeypatch.setattr("src.app._initialized", initialized)
    monkeypatch.setattr("src.app.check_connection", lambda: database)


class TestHealthy:
    def test_everything_up_returns_200(self, client, monkeypatch):
        set_state(monkeypatch)

        response = client.get("/health")

        assert response.status_code == 200
        assert json.loads(response.data)["status"] == "ok"


class TestUnhealthy:
    def test_database_down_returns_503(self, client, monkeypatch):
        set_state(monkeypatch, database=False)

        response = client.get("/health")

        assert response.status_code == 503, (
            "資料庫掛了卻回 200 的話，監控永遠不會叫——那是這條路徑唯一的用途"
        )
        body = json.loads(response.data)
        assert body["status"] == "degraded"
        assert body["checks"]["database"] is False

    def test_uninitialised_app_returns_503(self, client, monkeypatch):
        set_state(monkeypatch, initialized=False)

        response = client.get("/health")

        assert response.status_code == 503, "還沒初始化就不可能服務訊息"


class TestDoesNotLeak:
    """這條路徑是公開的，誰都打得到。"""

    def test_response_carries_nothing_but_the_verdict(self, client, monkeypatch):
        set_state(monkeypatch, database=False)

        body = json.loads(client.get("/health").data)

        assert set(body) == {"status", "checks"}
        assert set(body["checks"]) == {"initialized", "database"}
        assert all(isinstance(v, bool) for v in body["checks"].values()), (
            "只回布林值，不要把例外訊息或連線字串帶出去"
        )
