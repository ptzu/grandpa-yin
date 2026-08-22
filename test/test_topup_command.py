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
    env.send_text("點數")
    assert "儲值" in reply(env)


def test_points_query_stays_quiet_when_unavailable(with_liff):
    env = build_env(with_member_feature=True, with_payments=False)
    env.send_text("點數")
    assert "儲值" not in reply(env)


def _menu_labels(env):
    env.send_text("功能")
    return env.quick_reply


def test_menu_offers_topup_when_available(with_liff):
    env = build_env(with_payments=True)
    assert any("加購點數" in label for label in _menu_labels(env))


def test_menu_hides_topup_when_unavailable(with_liff):
    env = build_env(with_payments=False)
    assert not any("加購點數" in label for label in _menu_labels(env))


def test_menu_keeps_help_last(with_liff):
    """加購點數插在中間，使用說明仍要在最後"""
    labels = _menu_labels(build_env(with_payments=True))
    assert "使用說明" in labels[-1]
