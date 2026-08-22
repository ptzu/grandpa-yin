"""BillingService: the money flow around a background task.

The refund and pool-full paths had no coverage while this logic was buried in
ReplicateImageFeature — extracting it into a service made them reachable.
"""
import pytest

from conftest import (
    FakeMemberService, FakePublisher, build_env,
    billing_module, BillingService, EDIT_COST,
)


@pytest.fixture
def billing():
    member = FakeMemberService(points=100)
    publisher = FakePublisher()
    return BillingService(member, publisher), member, publisher


def submit(service, *, run, on_success=None, on_finish=None, points=5):
    delivered = []
    return service.submit(
        user_id="U-test-user",
        event=None,
        points=points,
        feature_type="edit",
        description="測試扣點",
        run=run,
        on_success=on_success or delivered.append,
        on_finish=on_finish,
    ), delivered


class TestHappyPath:
    def test_deducts_then_delivers(self, billing):
        service, member, publisher = billing

        queued, delivered = submit(service, run=lambda: "https://example.test/out.jpg")

        assert queued is True
        assert member.points == 95
        assert delivered == ["https://example.test/out.jpg"]
        assert member.refunds == []

    def test_on_finish_always_runs(self, billing):
        service, _, _ = billing
        finished = []

        submit(service, run=lambda: "ok", on_finish=lambda: finished.append(True))

        assert finished == [True]


class TestFailureRefunds:
    def test_failure_refunds_the_points(self, billing):
        service, member, publisher = billing

        submit(service, run=lambda: (_ for _ in ()).throw(RuntimeError("模型爆了")))

        assert member.points == 100, "失敗必須退回原本的點數"
        assert member.refunds == [{"amount": 5, "feature": "edit", "reason": "模型爆了"}]

    def test_failure_tells_the_user_points_came_back(self, billing):
        service, _, publisher = billing

        submit(service, run=lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        assert "退還" in (publisher.last["text"] or "")

    def test_failure_still_runs_on_finish(self, billing):
        service, _, _ = billing
        finished = []

        submit(service, run=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
               on_finish=lambda: finished.append(True))

        assert finished == [True], "失敗也要清狀態，否則用戶會卡在 processing"


class TestInsufficientPoints:
    def test_does_not_run_the_work_when_deduction_fails(self, billing):
        service, member, publisher = billing
        member.points = 1
        ran = []

        submit(service, run=lambda: ran.append(True), points=EDIT_COST)

        assert ran == [], "扣不到點就不該動用外部資源"
        assert "點數不夠" in (publisher.last["text"] or "")
        assert member.refunds == [], "沒扣到就沒得退"


class TestDeliveryFailure:
    """處理成功但送不到用戶手上——等於沒服務到，不能讓人白扣點。

    這條路徑實測會發生：影片訊息的縮圖網址不是 HTTPS 時 LINE 直接退件，
    當時扣了 25 點、用戶什麼也沒收到。
    """

    def test_failed_delivery_refunds(self, billing):
        service, member, publisher = billing

        submit(service, run=lambda: "ok", on_success=lambda r: False)

        assert member.points == 100, "沒送達就要把點數退回去"
        assert member.refunds == [{"amount": 5, "feature": "edit", "reason": "推送失敗"}]

    def test_failed_delivery_tells_the_user(self, billing):
        service, _, publisher = billing

        submit(service, run=lambda: "ok", on_success=lambda r: False)

        assert "退還" in (publisher.last["text"] or "")

    def test_successful_delivery_does_not_refund(self, billing):
        service, member, _ = billing

        submit(service, run=lambda: "ok", on_success=lambda r: True)

        assert member.refunds == []
        assert member.points == 95

    def test_on_success_returning_none_is_treated_as_delivered(self, billing):
        """只有明確回傳 False 才算失敗，None 不是"""
        service, member, _ = billing

        submit(service, run=lambda: "ok", on_success=lambda r: None)

        assert member.refunds == []


class TestCapacity:
    def test_pool_full_degrades_gracefully(self, billing, monkeypatch):
        service, member, publisher = billing
        monkeypatch.setattr(billing_module, "submit_image_task", lambda task: False)
        finished = []

        queued, _ = submit(service, run=lambda: "ok", on_finish=lambda: finished.append(True))

        assert queued is False
        assert member.points == 100, "沒排進去就不該扣點"
        assert "使用的人比較多" in (publisher.last["text"] or "")
        assert finished == [True], "狀態要清掉，否則用戶卡在 processing"


class TestThroughTheEditFeature:
    """同樣的退點行為，從真實的P圖大神流程走一遍"""

    def test_model_failure_refunds_and_clears_state(self):
        env = build_env(replicate_fails_with=RuntimeError("Replicate 掛了"))

        env.send_image()
        env.send_text("照我說的修改")
        env.send_text("加上彩虹")

        assert env.member.points == 100, "扣了又退，餘額應回到原點"
        assert env.member.refunds and env.member.refunds[0]["feature"] == "edit"
        assert "退還" in env.last_text
        assert env.state is None, "失敗後不能把用戶留在 processing"
        assert env.storage.objects == {}, "失敗也不留孤兒圖"
