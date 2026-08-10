#!/usr/bin/env python3
"""圖片流程狀態機的離線測試

不需要資料庫、LINE、Replicate、Supabase：全部以 fake 取代，直接驅動
FeatureRegistry 的路由，驗證「先傳圖 → 選功能 → 選描述 → 確認扣點」
整條路徑，以及取消／換圖／重新描述／點數不足等岔路。

    python3 test/test_image_flow.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Feature costs are read from env at construction time — pin them for determinism
os.environ["COLORIZE_COST"] = "10"
os.environ["EDIT_COST"] = "5"

from src.features import replicate_feature as replicate_module
from src.features.replicate_feature import ReplicateImageFeature
from src.features.feature_registry import FeatureRegistry
from src.features.menu_feature import MenuFeature
from src.features.colorize_feature import ColorizeFeature
from src.features.edit_feature import EditFeature
from src.features.photo_intent_feature import PhotoIntentFeature

FAKE_OUTPUT_URL = "https://example.test/output.jpg"
IMAGE_BYTES = b"\xff\xd8fake-jpeg"


# ---------------------------------------------------------------- fakes


class FakeStateManager:
    """記憶體版 UserStateManager，行為對齊 bot_sessions 的讀寫語意"""

    def __init__(self):
        self.states = {}

    def get_state(self, user_id):
        return self.states.get(user_id)

    def set_state(self, user_id, state):
        previous = self.states.get(user_id)
        replaced = dict(previous.get("data") or {}) if previous else None
        self.states[user_id] = {
            "feature": state.get("feature"),
            "state": state.get("state"),
            "data": state.get("data") or None,
        }
        return replaced

    def clear_state(self, user_id):
        removed = self.states.pop(user_id, None)
        if not removed:
            return None
        return dict(removed.get("data") or {})


class FakePublisher:
    def __init__(self):
        self.messages = []

    def _record(self, kind, message):
        quick_reply = getattr(message, "quick_reply", None)
        labels = []
        if quick_reply:
            labels = [item.action.label for item in quick_reply.items]
        self.messages.append({
            "kind": kind,
            "text": getattr(message, "text", None),
            "type": type(message).__name__,
            "quick_reply": labels,
        })

    def process_reply_message(self, reply_token, message, user_id, event=None):
        self._record("reply", message)

    def process_push_message(self, user_id, message, event=None):
        self._record("push", message)

    def reset(self):
        self.messages = []

    @property
    def last(self):
        return self.messages[-1] if self.messages else None

    def texts(self):
        return [m["text"] for m in self.messages if m["text"]]


class FakeContent:
    def iter_content(self):
        yield IMAGE_BYTES


class FakeLineBotApi:
    def get_message_content(self, message_id):
        return FakeContent()

    def get_profile(self, user_id):
        class Profile:
            display_name = "阿嬤"
        return Profile()


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self._counter = 0

    def is_configured(self):
        return True

    def upload_image(self, image_bytes, prefix="tmp"):
        self._counter += 1
        key = f"{prefix}/{self._counter}.jpg"
        self.objects[key] = image_bytes
        return key

    def download_image(self, key):
        return self.objects[key]

    def delete_image(self, key):
        self.objects.pop(key, None)
        self.deleted.append(key)


class FakeMemberService:
    def __init__(self, points=100):
        self.points = points
        self.deductions = []
        self.refunds = []

    def get_or_create_member(self, user_id, display_name=None):
        return {"points": self.points, "display_name": "阿嬤"}

    def get_member_info(self, user_id):
        return {"points": self.points, "display_name": "阿嬤"}

    def deduct_points(self, user_id, amount, description, feature_type=None):
        if self.points < amount:
            return False
        self.points -= amount
        self.deductions.append({"amount": amount, "feature": feature_type, "description": description})
        return True

    def refund_points(self, user_id, amount, feature_type=None, reason=None):
        self.points += amount
        self.refunds.append({"amount": amount, "feature": feature_type, "reason": reason})
        return True


# ------------------------------------------------------------- harness


USER = "U-test-user"


def build_registry(points=100):
    """照 app.py 的順序組裝 registry（photo_intent 必須最後註冊）"""
    line_bot_api = FakeLineBotApi()
    publisher = FakePublisher()
    state_manager = FakeStateManager()
    member_service = FakeMemberService(points)
    storage = FakeStorage()

    registry = FeatureRegistry(state_manager)
    registry.register(MenuFeature(line_bot_api, publisher, state_manager, member_service))
    registry.register(ColorizeFeature(line_bot_api, publisher, state_manager, member_service, storage))
    registry.register(EditFeature(line_bot_api, publisher, state_manager, member_service, storage))
    registry.register(PhotoIntentFeature(line_bot_api, publisher, state_manager, member_service, storage))

    return {
        "registry": registry,
        "publisher": publisher,
        "state": state_manager,
        "member": member_service,
        "storage": storage,
    }


def text_event(text, user_id=USER, source_type="user"):
    source = {"type": source_type, "userId": user_id}
    if source_type == "group":
        source["groupId"] = "G-test"
    return {
        "type": "message",
        "source": source,
        "replyToken": "reply-token",
        "message": {"type": "text", "id": "msg-text", "text": text},
    }


def image_event(user_id=USER, source_type="user"):
    source = {"type": source_type, "userId": user_id}
    if source_type == "group":
        source["groupId"] = "G-test"
    return {
        "type": "message",
        "source": source,
        "replyToken": "reply-token",
        "message": {"type": "image", "id": "msg-image"},
    }


def patch_externals():
    """把外部呼叫換掉：背景執行緒改同步跑、Replicate 與載入動畫改成 no-op"""
    replicate_module.submit_image_task = lambda task: (task(), True)[1]
    ReplicateImageFeature.start_loading_animation = lambda self, user_id: None
    ReplicateImageFeature.run_replicate = lambda self, input_dict: FAKE_OUTPUT_URL


# --------------------------------------------------------------- tests


RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    icon = "✅" if condition else "❌"
    print(f"  {icon} {name}" + (f"  — {detail}" if detail and not condition else ""))


def state_of(env):
    return env["state"].states.get(USER)


def test_photo_first_full_flow():
    print("\n[1] 先傳照片 → 選修改 → 選描述 → 確認 → 扣點出圖")
    env = build_registry()
    reg, pub = env["registry"], env["publisher"]

    reg.route_image_message(image_event())
    check("傳圖後有回覆（不再靜默丟棄）", len(pub.messages) == 1)
    check("回覆帶三個意圖選項", pub.last["quick_reply"] == ["📸 幫照片上色", "🎨 照我說的修改", "❌ 取消"],
          str(pub.last["quick_reply"]))
    check("狀態為 photo_intent:waiting_choice",
          state_of(env) and state_of(env)["feature"] == "photo_intent"
          and state_of(env)["state"] == "waiting_choice")
    check("照片已進 Storage", len(env["storage"].objects) == 1)

    pub.reset()
    reg.route_text_message(text_event("照我說的修改"))
    check("交棒給 edit 並進入等描述",
          state_of(env)["feature"] == "edit" and state_of(env)["state"] == "waiting_description")
    check("描述選單有預設選項可點", "🏖️ 背景換成海灘" in pub.last["quick_reply"], str(pub.last["quick_reply"]))
    check("交棒不重新上傳照片（沿用同一份暫存）", len(env["storage"].objects) == 1)
    check("此時尚未扣點", env["member"].points == 100)

    pub.reset()
    reg.route_text_message(text_event("背景換成海灘"))
    check("進入確認階段", state_of(env)["state"] == "waiting_confirm")
    check("確認訊息載明扣點數", "5 點" in (pub.last["text"] or ""), pub.last["text"])
    check("確認前仍未扣點", env["member"].points == 100)
    check("確認選單有三顆按鈕", pub.last["quick_reply"] == ["✅ 確定開始", "✏️ 重新描述", "❌ 取消"])

    pub.reset()
    patch_externals()
    reg.route_text_message(text_event("確定開始"))
    check("扣款一次且金額正確", env["member"].deductions == [
        {"amount": 5, "feature": "edit", "description": "圖片編輯：背景換成海灘"}
    ], str(env["member"].deductions))
    check("有推送結果圖片", any(m["type"] == "ImageSendMessage" for m in pub.messages))
    check("流程結束後狀態已清空", state_of(env) is None)
    check("暫存照片已刪除", env["storage"].objects == {})


def test_colorize_handoff():
    print("\n[2] 先傳照片 → 選上色")
    env = build_registry()
    reg, pub = env["registry"], env["publisher"]
    patch_externals()

    reg.route_image_message(image_event())
    pub.reset()
    reg.route_text_message(text_event("幫照片上色"))

    check("扣 colorize 的點數", env["member"].deductions == [
        {"amount": 10, "feature": "colorize", "description": "彩色化圖片"}
    ], str(env["member"].deductions))
    check("有推送結果圖片", any(m["type"] == "ImageSendMessage" for m in pub.messages))
    check("暫存照片已刪除", env["storage"].objects == {})
    check("狀態已清空", state_of(env) is None)


def test_cancel_before_charge():
    print("\n[3] 取消：任何階段都不扣點、不留垃圾")
    env = build_registry()
    reg, pub = env["registry"], env["publisher"]

    reg.route_image_message(image_event())
    reg.route_text_message(text_event("照我說的修改"))
    reg.route_text_message(text_event("背景換成海灘"))
    pub.reset()
    reg.route_text_message(text_event("取消"))

    check("完全沒有扣點", env["member"].deductions == [])
    check("有明確告知未扣點", "沒有扣" in (pub.last["text"] or ""), pub.last["text"])
    check("狀態已清空", state_of(env) is None)
    check("暫存照片已刪除", env["storage"].objects == {})


def test_redo_description_keeps_photo():
    print("\n[4] 重新描述：照片保留、不必重傳")
    env = build_registry()
    reg, pub = env["registry"], env["publisher"]

    reg.route_image_message(image_event())
    reg.route_text_message(text_event("照我說的修改"))
    reg.route_text_message(text_event("背景換成海灘"))
    key_before = state_of(env)["data"]["image_key"]

    pub.reset()
    reg.route_text_message(text_event("重新描述"))
    check("退回等描述階段", state_of(env)["state"] == "waiting_description")
    check("照片沒被丟掉", state_of(env)["data"].get("image_key") == key_before)
    check("舊描述已清掉", "description" not in state_of(env)["data"])
    check("再次給出描述選單", "🏖️ 背景換成海灘" in pub.last["quick_reply"])

    reg.route_text_message(text_event("天空變成夕陽"))
    check("新描述進入確認", state_of(env)["data"]["description"] == "天空變成夕陽")


def test_replace_photo_mid_flow():
    print("\n[5] 描述階段再傳一張照片 = 換圖")
    env = build_registry()
    reg, pub = env["registry"], env["publisher"]

    reg.route_image_message(image_event())
    reg.route_text_message(text_event("照我說的修改"))
    old_key = state_of(env)["data"]["image_key"]

    pub.reset()
    reg.route_image_message(image_event())
    new_key = state_of(env)["data"]["image_key"]

    check("有回覆而非靜默吃掉", len(pub.messages) == 1)
    check("告知已換照片", "換成" in (pub.last["text"] or ""), pub.last["text"])
    check("換上新照片", new_key != old_key)
    check("舊照片已從 Storage 刪除", old_key in env["storage"].deleted and old_key not in env["storage"].objects)
    check("仍停在等描述階段", state_of(env)["state"] == "waiting_description")


def test_text_while_waiting_image():
    print("\n[6] 等照片時打字：要有引導，不能沒反應")
    env = build_registry()
    reg, pub = env["registry"], env["publisher"]

    reg.route_text_message(text_event("圖片編輯"))
    check("進入等照片階段", state_of(env)["state"] == "waiting_image")

    pub.reset()
    reg.route_text_message(text_event("這個要怎麼用"))
    check("有回覆", len(pub.messages) == 1)
    check("引導用戶傳照片", "照片" in (pub.last["text"] or ""), str(pub.last))


def test_classic_flow_still_works():
    print("\n[7] 舊路徑（先打指令再傳圖）仍然可用")
    env = build_registry()
    reg, pub = env["registry"], env["publisher"]
    patch_externals()

    reg.route_text_message(text_event("圖片編輯"))
    reg.route_image_message(image_event())
    check("edit 自己收下照片（沒被 photo_intent 攔走）",
          state_of(env)["feature"] == "edit" and state_of(env)["state"] == "waiting_description")

    reg.route_text_message(text_event("加上彩虹"))
    reg.route_text_message(text_event("確定開始"))
    check("完成扣點", env["member"].deductions and env["member"].deductions[0]["feature"] == "edit")

    env2 = build_registry()
    reg2 = env2["registry"]
    reg2.route_text_message(text_event("圖片彩色化"))
    reg2.route_image_message(image_event())
    check("彩色化舊路徑仍直接處理", env2["member"].deductions
          and env2["member"].deductions[0]["feature"] == "colorize", str(env2["member"].deductions))


def test_insufficient_points_handoff():
    print("\n[8] 點數不足時交棒被擋下")
    env = build_registry(points=1)
    reg, pub = env["registry"], env["publisher"]

    reg.route_image_message(image_event())
    pub.reset()
    reg.route_text_message(text_event("照我說的修改"))

    check("告知點數不足", "點數不足" in (pub.last["text"] or ""), pub.last["text"])
    check("沒有扣點", env["member"].deductions == [])
    check("狀態已收拾乾淨", state_of(env) is None)
    check("暫存照片已刪除（不留孤兒檔）", env["storage"].objects == {})


def test_group_chat_stays_quiet():
    print("\n[9] 群組中的照片不主動搭話")
    env = build_registry()
    reg, pub = env["registry"], env["publisher"]

    reg.route_image_message(image_event(source_type="group"))
    check("完全沒有回覆", pub.messages == [], str(pub.messages))
    check("沒有建立狀態", state_of(env) is None)


def test_switching_feature_mid_flow():
    print("\n[10] 流程中途切換到另一個功能")
    env = build_registry()
    reg = env["registry"]

    reg.route_text_message(text_event("圖片編輯"))
    reg.route_image_message(image_event())
    check("先在 edit 流程中", state_of(env)["feature"] == "edit")
    abandoned_key = state_of(env)["data"]["image_key"]

    reg.route_text_message(text_event("圖片彩色化"))
    check("切到 colorize 而不是被當成編輯描述",
          state_of(env)["feature"] == "colorize", str(state_of(env)))
    check("被放棄的暫存圖已刪除（不留孤兒物件）",
          abandoned_key in env["storage"].deleted and env["storage"].objects == {},
          str(env["storage"].objects))


def test_no_orphans_across_all_paths():
    print("\n[12] 各種中斷路徑都不留孤兒圖")

    def run(name, steps):
        env = build_registry()
        reg = env["registry"]
        patch_externals()
        for step in steps:
            step(reg)
        check(f"{name} 之後 Storage 是乾淨的", env["storage"].objects == {},
              f"殘留 {list(env['storage'].objects)}")

    img = lambda reg: reg.route_image_message(image_event())
    say = lambda text: (lambda reg: reg.route_text_message(text_event(text)))

    run("走完完整流程", [img, say("照我說的修改"), say("加上彩虹"), say("確定開始")])
    run("意圖選單直接取消", [img, say("取消")])
    run("描述階段取消", [img, say("照我說的修改"), say("取消")])
    run("確認階段取消", [img, say("照我說的修改"), say("加上彩虹"), say("取消")])
    run("連傳三張只留最後一張再取消", [img, img, img, say("取消")])
    run("換圖後走完流程", [img, say("照我說的修改"), img, say("加上彩虹"), say("確定開始")])
    run("重新描述後走完流程",
        [img, say("照我說的修改"), say("加上彩虹"), say("重新描述"), say("天空變成夕陽"), say("確定開始")])
    run("中途改用選單功能", [img, say("照我說的修改"), say("圖片彩色化")])


def test_timestamp_parsing():
    print("\n[13] 清理腳本的時間解析")
    from scripts.cleanup_storage import parse_timestamp

    cases = [
        ("2026-08-09T12:34:56Z", True),
        ("2026-08-09T12:34:56.789Z", True),
        # Supabase 常回傳超過微秒精度的位數，3.9 的 fromisoformat 吃不下
        ("2026-08-09T12:34:56.1234567Z", True),
        ("2026-08-09T12:34:56+00:00", True),
        ("2026-08-09T12:34:56", True),
    ]
    for value, should_parse in cases:
        parsed = parse_timestamp(value)
        check(f"能解析 {value}", (parsed is not None) == should_parse, str(parsed))
        if parsed is not None:
            check(f"{value} 帶時區資訊", parsed.tzinfo is not None)

    check("空值回傳 None", parse_timestamp(None) is None)
    check("壞字串回傳 None（保守不刪）", parse_timestamp("not-a-date") is None)


def test_unknown_choice_reprompts():
    print("\n[11] 選單看不懂的回答會再問一次")
    env = build_registry()
    reg, pub = env["registry"], env["publisher"]

    reg.route_image_message(image_event())
    pub.reset()
    reg.route_text_message(text_event("蛤"))

    check("有回覆", len(pub.messages) == 1)
    check("重新給出選項", pub.last["quick_reply"] == ["📸 幫照片上色", "🎨 照我說的修改", "❌ 取消"])
    check("狀態不變", state_of(env)["state"] == "waiting_choice")


def main():
    print("=" * 60)
    print("圖片流程狀態機測試")
    print("=" * 60)

    for test in [
        test_photo_first_full_flow,
        test_colorize_handoff,
        test_cancel_before_charge,
        test_redo_description_keeps_photo,
        test_replace_photo_mid_flow,
        test_text_while_waiting_image,
        test_classic_flow_still_works,
        test_insufficient_points_handoff,
        test_group_chat_stays_quiet,
        test_switching_feature_mid_flow,
        test_unknown_choice_reprompts,
        test_no_orphans_across_all_paths,
        test_timestamp_parsing,
    ]:
        test()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 60)
    print(f"結果：{passed}/{total} 通過")
    print("=" * 60)

    if passed != total:
        print("\n失敗項目：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  ❌ {name}  {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
