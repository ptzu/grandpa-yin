"""End-to-end tests for the photo flow state machine.

Drives the real FeatureRegistry through the fakes in conftest.py: "send photo →
pick a feature → pick a description → get charged and delivered", plus every
branch off it (cancel, swap photo, insufficient points).
"""
import pytest

from conftest import ANIMATE_COST, COLORIZE_COST, EDIT_COST

CHOICE_BUTTONS = ["📸 幫照片上色", "🎬 讓照片動起來", "🎨 照我說的修改", "❌ 取消"]
PRESET_BUTTON = "🏖️ 背景換成海灘"


class TestPhotoFirstFlow:
    """先傳照片 → 選修改 → 選描述 → 直接扣點出圖"""

    def test_photo_gets_a_reply_with_choices(self, env):
        env.send_image()

        assert len(env.messages) == 1, "傳圖後必須有回覆，不能靜默丟棄"
        assert env.quick_reply == CHOICE_BUTTONS
        assert env.state_is("photo_intent", "waiting_choice")
        assert len(env.storage.objects) == 1, "照片應已進 Storage"

    def test_handoff_to_edit_keeps_the_same_stashed_photo(self, env):
        env.send_image()
        env.reset()

        env.send_text("照我說的修改")

        assert env.state_is("edit", "waiting_description")
        assert PRESET_BUTTON in env.quick_reply, "描述選單要有預設選項可點"
        assert len(env.storage.objects) == 1, "交棒不應重新上傳照片"
        assert env.member.points == 100, "此時尚未扣點"

    def test_description_charges_once_and_delivers(self, env):
        """拿到描述就直接開始做並扣點，不再多問一次確認"""
        env.send_image()
        env.send_text("照我說的修改")
        env.reset()

        env.send_text("背景換成海灘")

        assert env.member.deductions == [
            {"amount": EDIT_COST, "feature": "edit", "description": "P圖大神：背景換成海灘"}
        ]
        assert env.pushed_image(), "應推送結果圖片"
        assert env.state_is("followup", "offered"), "交付後接上「還要再做點什麼嗎」"
        assert env.stashed_objects == {}, "暫存照片要刪除"
        assert env.archived_objects, "成品要留著，用戶 30 天內回頭還看得到"


class TestColorizeHandoff:
    """先傳照片 → 選上色"""

    def test_colorize_charges_and_delivers(self, env):
        env.send_image()
        env.reset()

        env.send_text("幫照片上色")

        assert env.member.deductions == [
            {"amount": COLORIZE_COST, "feature": "colorize", "description": "彩色化圖片"}
        ]
        assert env.pushed_image()
        assert env.stashed_objects == {}
        assert env.state_is("followup", "offered")


class TestCancel:
    """取消：任何階段都不扣點、不留垃圾"""

    def test_cancel_at_description_stage(self, env):
        env.send_image()
        env.send_text("照我說的修改")
        env.reset()

        env.send_text("取消")

        assert env.member.deductions == []
        assert "沒有扣" in env.last_text, "要明確告知未扣點"
        assert env.state is None
        assert env.storage.objects == {}


class TestReplacePhoto:
    """描述階段再傳一張照片 = 換圖"""

    def test_new_photo_replaces_the_old_one(self, env):
        env.send_image()
        env.send_text("照我說的修改")
        old_key = env.state["data"]["image_key"]
        env.reset()

        env.send_image()
        new_key = env.state["data"]["image_key"]

        assert len(env.messages) == 1, "要有回覆而非靜默吃掉"
        assert "換成" in env.last_text
        assert new_key != old_key
        assert old_key in env.storage.deleted
        assert old_key not in env.storage.objects
        assert env.state_is("edit", "waiting_description")


class TestGuidance:
    """等照片時打字：要有引導，不能沒反應"""

    def test_text_while_waiting_image_gets_guidance(self, env):
        env.send_text("P圖大神")
        assert env.state_is("edit", "waiting_image")
        env.reset()

        env.send_text("這個要怎麼用")

        assert len(env.messages) == 1
        assert "照片" in env.last_text

    def test_unknown_choice_reprompts(self, env):
        env.send_image()
        env.reset()

        env.send_text("蛤")

        assert len(env.messages) == 1
        assert env.quick_reply == CHOICE_BUTTONS
        assert env.state_is("photo_intent", "waiting_choice"), "狀態不該改變"


class TestClassicFlow:
    """舊路徑（先打指令再傳圖）仍然可用"""

    def test_edit_command_then_photo(self, env):
        env.send_text("P圖大神")
        env.send_image()

        assert env.state_is("edit", "waiting_description"), "edit 應自己收下照片，沒被 photo_intent 攔走"

        env.send_text("加上彩虹")

        assert env.member.deductions
        assert env.member.deductions[0]["feature"] == "edit"

    def test_colorize_command_then_photo_processes_directly(self, env):
        env.send_text("修復老照片")
        env.send_image()

        assert env.member.deductions
        assert env.member.deductions[0]["feature"] == "colorize"


class TestGuards:
    def test_insufficient_points_blocks_handoff_and_cleans_up(self, make_env):
        env = make_env(points=1)
        env.send_image()
        env.reset()

        env.send_text("照我說的修改")

        assert "點數不夠" in env.last_text
        assert env.member.deductions == []
        assert env.state is None, "狀態要收拾乾淨"
        assert env.storage.objects == {}, "不留孤兒檔"

    def test_group_chat_photo_stays_quiet(self, env):
        env.send_image(source_type="group")

        assert env.messages == [], "群組中的照片不主動搭話"
        assert env.state is None


class TestFeatureSwitching:
    """流程中途切換到另一個功能"""

    def test_switching_mid_flow_discards_the_abandoned_photo(self, env):
        env.send_text("P圖大神")
        env.send_image()
        assert env.state["feature"] == "edit"
        abandoned_key = env.state["data"]["image_key"]

        env.send_text("修復老照片")

        assert env.state["feature"] == "colorize", "應切過去，而不是被當成編輯描述"
        assert abandoned_key in env.storage.deleted
        assert env.storage.objects == {}


# 各種中斷路徑都不留孤兒圖
ORPHAN_PATHS = [
    ("走完完整流程", ["img", "照我說的修改", "加上彩虹"]),
    ("意圖選單直接取消", ["img", "取消"]),
    ("描述階段取消", ["img", "照我說的修改", "取消"]),
    ("連傳三張只留最後一張再取消", ["img", "img", "img", "取消"]),
    ("換圖後走完流程", ["img", "照我說的修改", "img", "加上彩虹"]),
    ("中途改用選單功能", ["img", "照我說的修改", "修復老照片"]),
]


@pytest.mark.parametrize("name,steps", ORPHAN_PATHS, ids=[p[0] for p in ORPHAN_PATHS])
def test_no_orphaned_images(env, name, steps):
    for step in steps:
        if step == "img":
            env.send_image()
        else:
            env.send_text(step)

    assert env.stashed_objects == {}, \
        f"{name} 之後不該留下暫存圖，殘留 {list(env.stashed_objects)}"


class TestStashFallback:
    """Supabase 上傳失敗時，暫存圖要退回 base64，而不是讓整個流程炸掉。"""

    def test_upload_failure_falls_back_to_base64(self, env):
        edit = env.registry.get_feature_by_name("edit")

        def boom(*args, **kwargs):
            raise ConnectionError("supabase down")

        env.storage.upload_image = boom

        stash = edit.stash_image(b"raw-bytes")

        assert "image_key" not in stash, "上傳失敗不該留下 image_key"
        assert stash.get("image_data"), "應退回 base64 內嵌"

    def test_photo_flow_survives_storage_outage(self, env):
        """整條流程：上傳壞掉時，用戶仍收到「照片收到了」而不是錯誤。"""
        def boom(*args, **kwargs):
            raise ConnectionError("supabase down")

        env.storage.upload_image = boom

        env.send_text("P圖大神")
        env.send_image()

        assert env.state_is("edit", "waiting_description"), (
            f"上傳失敗應退回 base64 並繼續，實得：{env.state!r}"
        )
        assert "錯誤" not in env.last_text


class TestUploadButtons:
    """「請傳照片」提示要提供拍照／相簿按鈕，長輩不必自己找輸入框的「＋」。"""

    def test_edit_prompt_offers_camera_and_album(self, env):
        env.send_text("P圖大神")
        assert "拍照" in env.quick_reply
        assert "選照片" in env.quick_reply
        assert "❌ 取消" in env.quick_reply

    def test_animate_prompt_offers_upload_buttons(self, env):
        env.send_text("照片動起來")
        assert "拍照" in env.quick_reply
        assert "選照片" in env.quick_reply

    def test_colorize_prompt_offers_upload_without_cancel(self, env):
        env.send_text("修復老照片")
        assert "拍照" in env.quick_reply
        assert "選照片" in env.quick_reply
        assert "❌ 取消" not in env.quick_reply, "colorize 沒有取消流程，不放取消鈕"

    def test_waiting_image_reminder_repeats_upload_buttons(self, env):
        env.send_text("P圖大神")
        env.reset()
        env.send_text("嗨")  # 等圖片時亂打字
        assert "拍照" in env.quick_reply
        assert "選照片" in env.quick_reply


class TestFeatureEntryPrompt:
    """進入功能的第一句統一格式：先講選了什麼、扣幾點，再指引動作。"""

    def test_edit_entry_states_choice_and_cost(self, env):
        env.send_text("P圖大神")
        assert "你現在選擇了「P圖大神」" in env.last_text
        assert f"扣 {EDIT_COST} 點" in env.last_text

    def test_colorize_entry_states_choice_and_cost(self, env):
        env.send_text("修復老照片")
        assert "你現在選擇了「修復老照片」" in env.last_text
        assert f"扣 {COLORIZE_COST} 點" in env.last_text

    def test_animate_entry_states_choice_and_cost(self, env):
        env.send_text("照片動起來")
        assert "你現在選擇了「照片動起來」" in env.last_text
        assert f"扣 {ANIMATE_COST} 點" in env.last_text
