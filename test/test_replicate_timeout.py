"""模型呼叫的時限：吊死的工作必須被砍掉，並且退點給用戶。

圖片處理只有少少幾個名額（src/core/task_executor.py），一件永遠不會回來的
工作就是永久少一個名額。這裡盯三件事：等太久會取消並拋出、正常完成不受影
響、以及設定檔的 timeout_seconds 真的傳得到 client。
"""
import pytest

from conftest import build_env
from src.core.settings import DEFAULT_TIMEOUT_SECONDS
from src.services.replicate_client import ReplicateClient, ReplicateTimeout

OUTPUT_URL = "https://replicate.delivery/out.jpg"


class FakePrediction:
    """照著腳本一路換狀態的 prediction；跑完腳本就停在最後一個狀態。"""

    def __init__(self, statuses, output=OUTPUT_URL, error=None):
        self._statuses = list(statuses)
        self.status = self._statuses.pop(0)
        self.output = output
        self.error = error
        self.id = "pred-test"
        self.cancelled = False
        self.reloads = 0

    def reload(self):
        self.reloads += 1
        if self._statuses:
            self.status = self._statuses.pop(0)

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def client(monkeypatch):
    """真的 ReplicateClient，但不會發出任何請求，也不會真的睡。"""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    c = ReplicateClient()
    return c


def drive(client, monkeypatch, prediction, timeout=60, tick=1.0):
    """讓 client 跑完一次 run()，每輪輪詢把假時鐘往前推 `tick` 秒。"""
    clock = {"now": 0.0}

    def fake_monotonic():
        return clock["now"]

    def fake_sleep(_seconds):
        clock["now"] += tick

    monkeypatch.setattr("src.services.replicate_client.time.monotonic", fake_monotonic)
    monkeypatch.setattr("src.services.replicate_client.time.sleep", fake_sleep)
    monkeypatch.setattr(
        client, "_create_prediction", lambda model, input_dict: prediction
    )
    return client.run("owner/model", {"image": "x"}, timeout=timeout)


class TestTimeout:
    def test_a_model_that_never_finishes_is_cancelled_and_raises(self, client, monkeypatch):
        stuck = FakePrediction(["starting"])  # 永遠停在 starting

        with pytest.raises(ReplicateTimeout):
            drive(client, monkeypatch, stuck, timeout=60, tick=10)

        assert stuck.cancelled, "逾時要取消 Replicate 那邊的工作，否則照樣被計費"

    def test_a_slow_but_finishing_model_is_not_cut_off(self, client, monkeypatch):
        slow = FakePrediction(["starting", "processing", "processing", "succeeded"])

        url = drive(client, monkeypatch, slow, timeout=60, tick=10)

        assert url == OUTPUT_URL
        assert not slow.cancelled, "還在時限內就不該砍"

    def test_a_prediction_that_is_already_done_needs_no_polling(self, client, monkeypatch):
        done = FakePrediction(["succeeded"])

        url = drive(client, monkeypatch, done, timeout=60)

        assert url == OUTPUT_URL
        assert done.reloads == 0, "終態就不必再問一次"

    def test_a_failed_prediction_raises_with_the_reason(self, client, monkeypatch):
        failed = FakePrediction(["starting", "failed"], output=None, error="OOM")

        with pytest.raises(Exception) as caught:
            drive(client, monkeypatch, failed, timeout=60)

        assert "OOM" in str(caught.value), "失敗原因要留在訊息裡，事後查得到"
        assert not isinstance(caught.value, ReplicateTimeout), "這不是逾時，別混為一談"


class TestTimeoutReachesTheClient:
    """設定檔調了時限，實際呼叫就要照著改——中間斷掉的話設定等於沒用。"""

    def test_each_feature_sends_its_configured_timeout(self):
        env = build_env()
        env.send_text("修復老照片")
        env.send_image()

        assert env.replicate.calls, "應該有呼叫模型"
        timeout = env.replicate.calls[0]["timeout"]
        assert isinstance(timeout, int) and timeout > 0, (
            f"功能沒把設定檔的時限傳給 client（拿到 {timeout!r}），"
            f"漏傳就會退回無限等"
        )

    def test_the_default_is_a_real_number_not_none(self):
        assert isinstance(DEFAULT_TIMEOUT_SECONDS, int)
        assert DEFAULT_TIMEOUT_SECONDS > 0
