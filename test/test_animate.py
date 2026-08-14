"""照片動起來：確認才扣點、輸出影片、以及縮圖的生命週期。

縮圖那部分是這個功能最容易寫錯的地方：LINE 會在訊息推送**之後**才自己去抓
縮圖，所以那張圖不能跟其他暫存圖一樣處理完就刪——刪了就破圖。
"""
import pytest

from conftest import ANIMATE_COST, USER, build_env
from src.services.preview_store import set_public_base_url

CONFIRM_BUTTONS = ["✅ 確定開始", "❌ 取消"]


@pytest.fixture
def env():
    return build_env()


def start_and_send_photo(env):
    env.send_text("照片動起來")
    env.send_image()


class TestFlow:
    def test_trigger_asks_for_a_photo(self, env):
        env.send_text("照片動起來")

        assert env.state_is("animate", "waiting_image")
        assert "照片" in env.last_text
        assert f"{ANIMATE_COST} 點" in env.last_text, "要先講清楚會扣多少點"

    def test_photo_goes_to_confirmation_without_charging(self, env):
        start_and_send_photo(env)

        assert env.state_is("animate", "waiting_confirm")
        assert env.quick_reply == CONFIRM_BUTTONS
        assert env.member.deductions == [], "確認前不得扣點"

    def test_confirm_charges_and_delivers_a_video(self, env):
        start_and_send_photo(env)
        env.reset()

        env.send_text("確定開始")

        assert env.member.deductions == [
            {"amount": ANIMATE_COST, "feature": "animate", "description": "照片動起來"}
        ]
        pushed = [m for m in env.messages if m["kind"] == "push"]
        assert pushed, "應該有推送結果"
        assert pushed[-1]["type"] == "VideoSendMessage", f"應推影片，實得 {pushed[-1]['type']}"
        assert env.state is None

    def test_cancel_costs_nothing_and_cleans_up(self, env):
        start_and_send_photo(env)
        env.reset()

        env.send_text("取消")

        assert env.member.deductions == []
        assert "沒有扣" in env.last_text
        assert env.state is None
        assert env.storage.objects == {}, "取消要把暫存照片刪掉"

    def test_no_motion_choice_is_offered(self, env):
        """刻意不讓用戶選動作——動作越大臉越容易崩"""
        start_and_send_photo(env)

        assert env.quick_reply == CONFIRM_BUTTONS, "只該有確認/取消，不給動作選項"


class TestThumbnailLifecycle:
    """LINE 推送後才會自己去抓縮圖，所以那張圖不能當場刪"""

    def test_thumbnail_uses_a_signed_url(self, env):
        start_and_send_photo(env)
        env.send_text("確定開始")

        assert env.storage.signed, "應該為縮圖產生 signed URL"
        key, expires = env.storage.signed[0]
        assert expires >= 3600, "縮圖網址要撐得夠久讓 LINE 來抓"

    def test_thumbnail_survives_completion(self, env):
        start_and_send_photo(env)
        key = env.state["data"]["image_key"]

        env.send_text("確定開始")

        assert key in env.storage.objects, "縮圖在流程結束後仍須存在，否則影片訊息破圖"
        assert key not in env.storage.deleted

    def test_serves_thumbnail_locally_when_storage_is_absent(self, monkeypatch):
        """沒有 Storage 時改由本服務供圖（本地開發走 ngrok，不依賴雲端）"""
        env = build_env()
        monkeypatch.setattr(env.storage, "is_configured", lambda: False)
        set_public_base_url("https://test.ngrok-free.app")

        start_and_send_photo(env)
        env.send_text("確定開始")

        pushed = [m for m in env.messages if m["kind"] == "push"]
        assert pushed[-1]["type"] == "VideoSendMessage", "沒有 Storage 也要做得出影片"
        assert env.member.deductions, "這條路徑要照常扣點"
        assert env.storage.signed == [], "沒有 Storage 就不該去簽 URL"

    def test_locally_served_thumbnail_is_retrievable(self, monkeypatch):
        """本地縮圖必須真的取得回來，否則 LINE 會拿到破圖"""
        env = build_env()
        monkeypatch.setattr(env.storage, "is_configured", lambda: False)
        set_public_base_url("https://test.ngrok-free.app")

        start_and_send_photo(env)
        env.send_text("確定開始")

        import os
        tokens = os.listdir(env.preview_store.directory)
        assert tokens, "應該有一張本地縮圖"
        assert env.preview_store.load(tokens[0]), "縮圖必須讀得回來"

    def test_rejects_bogus_preview_tokens(self, env):
        """token 是固定格式的隨機 hex——擋掉路徑穿越與亂猜"""
        assert env.preview_store.load("../../../etc/passwd") is None
        assert env.preview_store.load("not-a-token") is None
        assert env.preview_store.load("") is None

    def test_refuses_non_https_preview_url(self, monkeypatch):
        """LINE 拒收非 HTTPS 的媒體網址——與其送出去被退（用戶白扣點），
        不如當場放棄。這是實測踩到的坑：ngrok 轉進來是 http。"""
        env = build_env()
        monkeypatch.setattr(env.storage, "is_configured", lambda: False)
        set_public_base_url("http://localhost:5000")

        start_and_send_photo(env)
        env.reset()
        env.send_text("確定開始")

        assert env.member.deductions == [], "做不出合規網址就不該扣點"
        assert "錯誤" in env.last_text

    def test_delivery_failure_refunds_the_points(self, monkeypatch):
        """萬一還是被 LINE 退件，點數要退回去"""
        env = build_env()
        env.publisher.push_fails = True

        start_and_send_photo(env)
        env.send_text("確定開始")

        assert env.member.points == 100, "白扣點是不能接受的"
        assert env.member.refunds and env.member.refunds[0]["reason"] == "推送失敗"

    def test_fails_cleanly_when_no_public_url_is_known(self, monkeypatch):
        """既沒 Storage 也不知道自己的公開網址 → 明講且不扣點，不推破圖"""
        env = build_env()
        monkeypatch.setattr(env.storage, "is_configured", lambda: False)
        set_public_base_url(None)

        start_and_send_photo(env)
        env.reset()
        env.send_text("確定開始")

        assert env.member.deductions == [], "做不出來就不能扣點"
        assert "錯誤" in env.last_text
        assert env.state is None


class TestModelWiring:
    def test_sends_the_configured_model_and_default_prompt(self, env):
        start_and_send_photo(env)

        env.send_text("確定開始")

        call = env.replicate.calls[0]
        assert call["model"] == "prunaai/p-video"
        assert isinstance(call["input"]["image"], str), "p-video 吃單值，不是陣列"
        assert call["input"]["prompt"], "應自動帶入設定檔的 default_prompt"
        assert call["input"]["duration"] == 5


class TestGuards:
    def test_insufficient_points_blocks_before_the_photo(self):
        env = build_env(points=1)

        env.send_text("照片動起來")

        assert "點數不足" in env.last_text
        assert env.state is None

    def test_model_failure_refunds(self):
        env = build_env(replicate_fails_with=RuntimeError("影片模型掛了"))
        start_and_send_photo(env)
        env.reset()

        env.send_text("確定開始")

        assert env.member.points == 100, "扣了又退，餘額回到原點"
        assert env.member.refunds and env.member.refunds[0]["feature"] == "animate"
        assert env.state is None


class TestPhotoIntentHandoff:
    """主要入口：直接傳照片 → 選「讓照片動起來」"""

    def test_handoff_from_photo_intent(self, env):
        env.send_image()
        assert env.state_is("photo_intent", "waiting_choice")
        env.reset()

        env.send_text("讓照片動起來")

        assert env.state_is("animate", "waiting_confirm")
        assert env.member.deductions == [], "交棒後仍要確認才扣點"
        assert len(env.storage.objects) == 1, "沿用同一份暫存，不重新上傳"

    def test_choice_menu_offers_animate(self, env):
        env.send_image()

        assert "🎬 讓照片動起來" in env.quick_reply
