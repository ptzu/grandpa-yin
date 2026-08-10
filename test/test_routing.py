"""Tests for FeatureRegistry routing rules.

These pin down the contracts that are otherwise only guarded by comments —
registration order as priority, the photo catch-all having to be registered
last, and global commands cutting through an in-progress flow.
"""
import pytest

from conftest import build_env, text_event, image_event, USER


class TestRegistrationOrder:
    def test_photo_intent_is_registered_last(self, env):
        """catch-all 必須最後註冊，否則會搶在 colorize / edit 之前接走圖片"""
        names = [f.name for f in env.registry.get_all_features()]
        assert names[-1] == "photo_intent", f"photo_intent 必須是最後一個，目前順序：{names}"

    def test_photo_intent_accepts_any_image(self, env):
        photo_intent = env.registry.get_feature_by_name("photo_intent")
        assert photo_intent.can_handle_image(USER) is True, "catch-all 必須永遠收得下圖片"

    def test_a_feature_in_state_wins_over_the_catch_all(self, env):
        env.send_text("圖片編輯")
        env.send_image()
        assert env.state["feature"] == "edit", "有狀態的功能優先於 catch-all"


class TestGlobalCommands:
    def test_global_command_works_mid_flow(self):
        """卡在編輯流程中途時，「點數」仍要查得到點數"""
        env = build_env(with_member_feature=True)
        env.send_text("圖片編輯")
        env.send_image()
        env.reset()

        env.send_text("點數")

        assert "剩餘點數" in env.last_text, f"全局命令應被 member 接走，實得：{env.last_text!r}"

    def test_global_command_does_not_destroy_the_flow(self):
        env = build_env(with_member_feature=True)
        env.send_text("圖片編輯")
        env.send_image()
        before = env.state

        env.send_text("點數")

        assert env.state == before, "查點數不該把用戶踢出編輯流程"

    def test_menu_command_works_mid_flow(self, env):
        env.send_text("圖片編輯")
        env.reset()

        env.send_text("!功能")

        assert "請選擇您想要的功能" in env.last_text


class TestTriggerCommands:
    def test_other_trigger_command_is_not_swallowed_as_input(self, env):
        """在等描述時輸入別的功能指令，應切換功能而不是被當成描述"""
        env.send_image()
        env.send_text("照我說的修改")

        env.send_text("圖片彩色化")

        assert env.state["feature"] == "colorize"

    def test_free_text_in_description_stage_is_treated_as_description(self, env):
        env.send_image()
        env.send_text("照我說的修改")

        env.send_text("把背景換成公園")

        assert env.state["state"] == "waiting_confirm"
        assert env.state["data"]["description"] == "把背景換成公園"


class TestUnroutableMessages:
    def test_unknown_text_without_state_is_ignored(self, env):
        env.send_text("今天天氣真好")

        assert env.messages == [], "沒有功能能處理時應安靜，不亂回覆"
        assert env.state is None

    def test_registry_returns_none_when_nothing_handles(self, env):
        assert env.registry.route_text_message(text_event("完全沒人管的訊息")) is None


class TestStateLookups:
    """已知缺口：路由層查好的狀態沒有傳進 handle_*，功能內又查一次 DB。

    標記為 xfail 而不是刪掉——這是階段 3（路由層收斂）要修的，測試先寫著，
    修好的那天把 xfail 拿掉就有回歸保護。
    """

    @pytest.mark.xfail(reason="handle_text 不接收路由層查好的 state，會重複查一次 DB")
    def test_state_is_read_once_per_text_message(self, env, monkeypatch):
        """路由層查一次狀態就該夠用；重複查代表每則訊息多打一次 DB"""
        env.send_text("圖片編輯")

        calls = []
        original = env.state_manager.get_state
        monkeypatch.setattr(env.state_manager, "get_state",
                            lambda user_id: (calls.append(user_id), original(user_id))[1])

        env.registry.route_text_message(text_event("隨便打的描述"))

        assert len(calls) == 1, f"每則文字訊息只該查一次狀態，實際查了 {len(calls)} 次"

    @pytest.mark.xfail(reason="can_handle_image / handle_image 各自再查一次 DB")
    def test_state_is_read_once_per_image_message(self, env, monkeypatch):
        env.send_text("圖片編輯")

        calls = []
        original = env.state_manager.get_state
        monkeypatch.setattr(env.state_manager, "get_state",
                            lambda user_id: (calls.append(user_id), original(user_id))[1])

        env.registry.route_image_message(image_event())

        assert len(calls) == 1, f"每則圖片訊息只該查一次狀態，實際查了 {len(calls)} 次"
