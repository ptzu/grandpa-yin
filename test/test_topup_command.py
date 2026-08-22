"""The 「儲值」command and where top-up shows up in the UI.

The rule these tests protect: never show an elderly user a top-up entry that
leads nowhere. Both halves — a configured gateway and a LIFF page — have to be
present, or the bot stays quiet about topping up.
"""
import pytest

from conftest import build_env

LIFF_ID = "1234567890-abcdefgh"


@pytest.fixture
def with_liff(monkeypatch):
    monkeypatch.setenv("LIFF_ID", LIFF_ID)


@pytest.fixture
def without_liff(monkeypatch):
    monkeypatch.delenv("LIFF_ID", raising=False)


def reply(env):
    return env.last_text


# ---------------------------------------------------------------- 儲值 command


def test_topup_gives_link_and_plans(with_liff):
    import json
    env = build_env(with_member_feature=True, with_payments=True)
    env.send_text("儲值")

    msg = env.publisher.messages[-1]
    assert msg["type"] == "FlexSendMessage", "儲值回覆是 Flex 卡片"
    blob = json.dumps(msg["message"].as_json_dict(), ensure_ascii=False)
    assert f"liff.line.me/{LIFF_ID}" in blob, "按鈕帶去加購入口"
    assert "NT$100" in blob and "NT$250" in blob
    assert "300 點" in blob, "設定檔的 label 要照樣顯示"


def test_topup_shows_current_balance(with_liff):
    env = build_env(points=42, with_member_feature=True, with_payments=True)
    env.send_text("儲值")
    assert "42 點" in env.publisher.messages[-1]["alt_text"]


def test_topup_says_so_when_gateway_not_configured(with_liff):
    """沒接金流時要明講，不能沉默或給壞連結"""
    env = build_env(with_member_feature=True, with_payments=False)
    env.send_text("儲值")

    text = reply(env)
    assert "還沒開放" in text
    assert "liff.line.me" not in text


def test_topup_says_so_when_liff_missing(without_liff):
    """金流設好但 LIFF 沒開通——連結會通往空白頁，一樣不能給"""
    env = build_env(with_member_feature=True, with_payments=True)
    env.send_text("儲值")

    text = reply(env)
    assert "還沒開放" in text
    assert "liff.line.me" not in text


# ---------------------------------------------------------------- entry points


def test_points_query_mentions_topup_when_available(with_liff):
    env = build_env(with_member_feature=True, with_payments=True)
    env.send_text("會員中心")
    assert "儲值" in reply(env)


def test_points_query_stays_quiet_when_unavailable(with_liff):
    env = build_env(with_member_feature=True, with_payments=False)
    env.send_text("會員中心")
    assert "儲值" not in reply(env)


def _menu_labels(env):
    env.send_text("功能")
    return env.quick_reply


def test_menu_shows_three_features_and_member_center(with_liff):
    """功能選單只留三個圖片功能與會員中心"""
    labels = _menu_labels(build_env(with_member_feature=True, with_payments=True))
    assert any("修復老照片" in l for l in labels)
    assert any("照片動起來" in l for l in labels)
    assert any("P圖大神" in l for l in labels)
    assert any("會員中心" in l for l in labels)
    assert len(labels) == 4, f"選單應只有四顆，實得：{labels}"


def test_menu_no_longer_shows_topup_points_or_help(with_liff):
    """儲值／點數／使用說明都收進會員中心，不再直接掛在功能選單"""
    labels = _menu_labels(build_env(with_member_feature=True, with_payments=True))
    assert not any("加購點數" in l for l in labels)
    assert not any("我的點數" in l for l in labels)
    assert not any("使用說明" in l for l in labels)


# ---------------------------------------------------------------- 會員中心

class TestMemberCenter:
    """「會員中心」回覆要帶儲值（有開放才給）與使用說明的按鈕。"""

    def test_member_center_offers_topup_and_help(self, with_liff):
        env = build_env(with_member_feature=True, with_payments=True)
        env.send_text("會員中心")
        assert "💎 儲值" in env.quick_reply
        assert "❓ 使用說明" in env.quick_reply

    def test_member_center_hides_topup_when_unavailable(self):
        env = build_env(with_member_feature=True, with_payments=False)
        env.send_text("會員中心")
        assert "💎 儲值" not in env.quick_reply, "沒開放儲值就不給按鈕"
        assert "❓ 使用說明" in env.quick_reply

    def test_old_command_no_longer_works(self):
        env = build_env(with_member_feature=True)
        env.send_text("會員")
        assert env.messages == [], "舊指令「會員」已改名，不該再有回應"

    def test_member_center_shows_works_summary(self):
        env = build_env(with_member_feature=True)
        env.member.works = {"colorize": 3, "animate": 2}
        env.send_text("會員中心")
        text = reply(env)
        assert "上色 3 張" in text
        assert "影片 2 支" in text

    def test_member_center_encourages_when_no_works(self):
        env = build_env(with_member_feature=True)
        env.member.works = {}
        env.send_text("會員中心")
        assert "還沒有作品" in reply(env)
