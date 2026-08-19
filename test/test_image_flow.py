"""End-to-end tests for the photo flow state machine.

Drives the real FeatureRegistry through the fakes in conftest.py: "send photo →
pick a feature → pick a description → confirm and get charged", plus every
branch off it (cancel, swap photo, redo description, insufficient points).
"""
import pytest

from conftest import COLORIZE_COST, EDIT_COST

CHOICE_BUTTONS = ["📸 幫照片上色", "🎬 讓照片動起來", "🎨 照我說的修改", "❌ 取消"]
CONFIRM_BUTTONS = ["✅ 確定開始", "✏️ 重新描述", "❌ 取消"]
PRESET_BUTTON = "🏖️ 背景換成海灘"


class TestPhotoFirstFlow:
    """先傳照片 → 選修改 → 選描述 → 確認 → 扣點出圖"""

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

    def test_description_moves_to_confirm_without_charging(self, env):
        env.send_image()
        env.send_text("照我說的修改")
        env.reset()

        env.send_text("背景換成海灘")

        assert env.state_is("edit", "waiting_confirm")
        assert f"{EDIT_COST} 點" in env.last_text, "確認訊息要載明扣點數"
        assert env.member.points == 100, "確認前不得扣點"
        assert env.quick_reply == CONFIRM_BUTTONS

    def test_confirm_charges_once_and_delivers(self, env):
        env.send_image()
        env.send_text("照我說的修改")
        env.send_text("背景換成海灘")
        env.reset()

        env.send_text("確定開始")

        assert env.member.deductions == [
            {"amount": EDIT_COST, "feature": "edit", "description": "圖片編輯：背景換成海灘"}
        ]
        assert env.pushed_image(), "應推送結果圖片"
        assert env.state is None, "流程結束後狀態要清空"
        assert env.storage.objects == {}, "暫存照片要刪除"


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
        assert env.storage.objects == {}
        assert env.state is None


class TestCancel:
    """取消：任何階段都不扣點、不留垃圾"""

    def test_cancel_at_confirm_stage(self, env):
        env.send_image()
        env.send_text("照我說的修改")
        env.send_text("背景換成海灘")
        env.reset()

        env.send_text("取消")

        assert env.member.deductions == []
        assert "沒有扣" in env.last_text, "要明確告知未扣點"
        assert env.state is None
        assert env.storage.objects == {}


class TestRedoDescription:
    """重新描述：照片保留、不必重傳"""

    def test_redo_keeps_photo_and_drops_old_description(self, env):
        env.send_image()
        env.send_text("照我說的修改")
        env.send_text("背景換成海灘")
        key_before = env.state["data"]["image_key"]
        env.reset()

        env.send_text("重新描述")

        assert env.state_is("edit", "waiting_description")
        assert env.state["data"].get("image_key") == key_before, "照片不該被丟掉"
        assert "description" not in env.state["data"], "舊描述要清掉"
        assert PRESET_BUTTON in env.quick_reply

        env.send_text("天空變成夕陽")
        assert env.state["data"]["description"] == "天空變成夕陽"


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
        env.send_text("圖片編輯")
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
        env.send_text("圖片編輯")
        env.send_image()

        assert env.state_is("edit", "waiting_description"), "edit 應自己收下照片，沒被 photo_intent 攔走"

        env.send_text("加上彩虹")
        env.send_text("確定開始")

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
        env.send_text("圖片編輯")
        env.send_image()
        assert env.state["feature"] == "edit"
        abandoned_key = env.state["data"]["image_key"]

        env.send_text("修復老照片")

        assert env.state["feature"] == "colorize", "應切過去，而不是被當成編輯描述"
        assert abandoned_key in env.storage.deleted
        assert env.storage.objects == {}


# 各種中斷路徑都不留孤兒圖
ORPHAN_PATHS = [
    ("走完完整流程", ["img", "照我說的修改", "加上彩虹", "確定開始"]),
    ("意圖選單直接取消", ["img", "取消"]),
    ("描述階段取消", ["img", "照我說的修改", "取消"]),
    ("確認階段取消", ["img", "照我說的修改", "加上彩虹", "取消"]),
    ("連傳三張只留最後一張再取消", ["img", "img", "img", "取消"]),
    ("換圖後走完流程", ["img", "照我說的修改", "img", "加上彩虹", "確定開始"]),
    ("重新描述後走完流程",
     ["img", "照我說的修改", "加上彩虹", "重新描述", "天空變成夕陽", "確定開始"]),
    ("中途改用選單功能", ["img", "照我說的修改", "修復老照片"]),
]


@pytest.mark.parametrize("name,steps", ORPHAN_PATHS, ids=[p[0] for p in ORPHAN_PATHS])
def test_no_orphaned_images(env, name, steps):
    for step in steps:
        if step == "img":
            env.send_image()
        else:
            env.send_text(step)

    assert env.storage.objects == {}, f"{name} 之後 Storage 應是乾淨的，殘留 {list(env.storage.objects)}"
