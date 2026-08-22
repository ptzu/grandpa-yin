"""The 「兌換」conversation and where gift cards show up in the UI.

The rule these tests protect is the same one top-up follows: never show an
elderly user an entry that leads nowhere, and never leave them stuck. A wrong
code has to be re-typeable without starting over, and「功能」has to get them
out of the flow at any point.
"""
import pytest

from conftest import build_env

CODE = "ABCD1234"
PRETTY = "ABCD-1234"


def reply(env):
    return env.last_text


def gift_env(points=100, cards=None, **kwargs):
    return build_env(points=points, with_member_feature=True, with_gift=True,
                     gift_cards=cards if cards is not None else {CODE: 300},
                     **kwargs)


# ------------------------------------------------------------- 兌換 command


def test_bare_command_asks_for_the_code():
    env = gift_env()
    env.send_text("兌換")

    assert "卡號" in reply(env)
    assert env.state["feature"] == "gift"


def test_code_on_the_same_line_redeems_in_one_step():
    """子女傳的連結會帶「兌換 ABCD-1234」進來，長輩只要按送出"""
    env = gift_env()
    env.send_text(f"兌換 {PRETTY}")

    text = reply(env)
    assert "300 點" in text
    assert env.state is None, "兌換完就該離開流程"


def test_code_typed_at_the_prompt_redeems():
    env = gift_env()
    env.send_text("兌換")
    env.send_text(PRETTY)

    assert "300 點" in reply(env)
    assert env.state is None


def test_balance_after_redeeming_is_shown():
    env = gift_env()
    env.send_text(f"兌換 {CODE}")
    assert "300 點" in reply(env)


@pytest.mark.parametrize("typed", ["abcd-1234", "ABCD1234", " abcd 1234 "])
def test_how_they_type_it_does_not_matter(typed):
    env = gift_env()
    env.send_text("兌換")
    env.send_text(typed)

    assert "300 點" in reply(env)


# ------------------------------------------------------------------ failures


def test_used_card_says_so_plainly():
    env = gift_env()
    env.send_text(f"兌換 {CODE}")
    env.send_text(f"兌換 {CODE}")

    assert "用過" in reply(env)


def test_wrong_code_keeps_the_prompt_open():
    """打錯不必從頭來過——留在等卡號的狀態直接重打"""
    env = gift_env()
    env.send_text("兌換")
    env.send_text("ZZZZ9999")

    assert "找不到" in reply(env)
    assert env.state["state"] == "waiting_code"

    env.send_text(PRETTY)
    assert "300 點" in reply(env)


def test_repeated_failures_send_them_back_to_the_giver():
    """連錯三次多半是看錯了東西，再叫他重打沒有意義。

    「兌換」文字流程現在只是隱藏退路——正常收禮是點卡片一鍵領取——但退路
    仍要好好收尾，連錯了就請他回頭找送禮的人。"""
    env = gift_env()
    env.send_text("兌換")
    for _ in range(3):
        env.send_text("ZZZZ9999")

    text = reply(env)
    assert "朋友" in text
    assert env.state is None


def test_cancel_leaves_the_flow():
    env = gift_env()
    env.send_text("兌換")
    env.send_text("取消")

    assert env.state is None


def test_global_commands_still_escape_the_prompt():
    """等卡號時打「功能」要跳得出去，不能被當成卡號吃掉"""
    env = gift_env()
    env.send_text("兌換")
    env.send_text("功能")

    assert "想做什麼" in reply(env)


# ------------------------------------------------------------------ the UI


def test_menu_has_no_manual_redeem_button():
    """收禮改成點卡片一鍵領取，選單不該再出現要人手動輸入卡號的兌換入口"""
    env = gift_env()
    env.send_text("功能")

    assert not any("兌換" in label for label in env.quick_reply)


def test_help_explains_one_tap_claiming_not_codes():
    """使用說明講的是「點卡片就收下」，不是「輸入兌換再打卡號」"""
    env = gift_env()
    env.send_text("使用說明")

    text = reply(env)
    assert "點一下" in text
    assert "兌換" not in text and "卡號" not in text


# --------------------------------------------------------- 買點數的入口只有一個
#
# 「儲值」給一條連結就好，自用或送禮在那一頁裡選。訊息裡放兩條連結會讓長輩
# 卡在「該點哪一個」——而「幫誰買」是他們一看就能回答的問題。

BASE_URL = "https://grandpa.example"
GIFT_URL = f"{BASE_URL}/gift"


@pytest.fixture
def with_public_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setenv("LIFF_ID", "1234567890-abcdefgh")


@pytest.fixture
def without_public_url(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("LIFF_ID", "1234567890-abcdefgh")


@pytest.fixture
def without_liff(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.delenv("LIFF_ID", raising=False)


def test_topup_gives_exactly_one_link(with_public_url):
    """儲值卡片只有一顆按鈕、一條連結，指向「幫誰買」的分流入口"""
    import json
    env = gift_env(with_payments=True)
    env.send_text("儲值")

    msg = env.publisher.messages[-1]
    assert msg["type"] == "FlexSendMessage"
    blob = json.dumps(msg["message"].as_json_dict(), ensure_ascii=False)
    assert blob.count("liff.line.me") == 1, "只該有一條連結（去加購按鈕）"
    assert "?p=start" in blob, "連結要指向「幫誰買」的分流頁"


def test_points_query_points_at_the_command_not_a_url(with_public_url):
    """查點數時給指令就好——網址留給真的要買的那一頁"""
    env = gift_env(with_payments=True)
    env.send_text("會員中心")

    text = reply(env)
    assert "儲值" in text
    assert "https://" not in text


def test_running_out_of_points_still_gives_a_way_out(with_public_url):
    """『點數不夠』如果沒給出路就是死路"""
    env = gift_env(points=1, with_payments=True)
    env.send_image()
    env.reset()
    env.send_text("照我說的修改")

    text = reply(env)
    assert "點數不夠" in text
    assert "儲值" in text


def test_without_liff_the_plain_gift_url_is_offered_instead(without_liff):
    """LIFF 沒開通時 bot 講不出分流頁，改給家人也開得起來的一般網頁"""
    env = gift_env(with_payments=True)
    env.send_text("會員中心")

    assert GIFT_URL in reply(env)


def test_nothing_is_offered_without_a_gateway(with_public_url):
    """金流沒接的時候，兩條路都不能提"""
    env = gift_env(with_payments=False)
    env.send_text("會員中心")

    text = reply(env)
    assert "/gift" not in text and "儲值" not in text


# ------------------------------------------------------------- 收禮的那則通知


def test_redeeming_reads_like_receiving_a_gift():
    """這則訊息就是長輩看到的「收到禮物」通知，不是交易確認"""
    env = gift_env()
    env.send_text(f"兌換 {PRETTY}")

    text = reply(env)
    assert "禮物" in text
    assert "300 點" in text
    # 收完禮馬上有事可做，不是死路
    assert any("修復老照片" in label for label in env.quick_reply)


# --------------------------------------------------- 分享頁：誰能點什麼的規矩
#
# 這幾條看的是樣板檔本身。它們釘住的是一個很容易再犯的錯：把「開啟 bot 並帶入
# 兌換訊息」的深連結，放到「買家自己在看」的頁面上——買家點一下就把禮物兌換到
# 自己帳上了。同一個連結放進「要送出去的卡片」裡才是對的。

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "src" / "templates"
APP_PY = ROOT / "src" / "app.py"


def test_done_page_links_to_the_share_page_without_liff():
    """完成頁被綠界導回時在外部瀏覽器：在那裡 init LIFF 會把頁面 redirect 掉。
    所以完成頁自己不碰 LIFF，只用一條 liff.line.me 連結把人帶回 LINE 內的分享
    頁（那裡才有 LIFF 情境，卡片選擇器才能用）。"""
    html = (TEMPLATES / "gift_done.html").read_text(encoding="utf-8")

    assert "liff.line.me/" in html and "?p=share&no=" in html, "用 liff.line.me 連結帶回分享頁"
    assert "window.location.href = link" in html, "卡號就緒後自動跳進 LIFF，零點擊彈卡片"
    assert "static.line-scdn.net/liff" not in html, "完成頁不該載入 LIFF SDK（會 redirect）"
    assert "禮物卡號" not in html, "完成頁不顯示卡號"


def test_share_page_puts_the_claim_link_inside_the_shared_card():
    """分享頁相反：兌換深連結要包進送出去的卡片，收禮的人點才對"""
    html = (TEMPLATES / "gift_share.html").read_text(encoding="utf-8")

    assert "shareTargetPicker" in html
    assert "line.me/R/oaMessage" in html, "卡片裡要有讓長輩一點就開啟 bot 的連結"
    # 沒有 basic id 就組不出深連結，此時卡片要退回純文字說明而不是壞按鈕
    assert "if (!url)" in html


def test_share_page_handles_a_device_without_the_picker():
    """挑不了好友的裝置要給得出退路——引導重試，且從不露出卡號"""
    html = (TEMPLATES / "gift_share.html").read_text(encoding="utf-8")
    assert "isApiAvailable" in html
    # 卡號從此不在任何送禮方看得到的地方出現
    assert "禮物卡號" not in html and "revealCode" not in html
    # 「複製收禮連結」按鈕已移除，頁面更乾淨
    assert "copyClaimLink" not in html and "複製收禮連結" not in html


def test_liff_links_select_the_page_by_query_not_by_path():
    """一個 LIFF app 只能設一個 Endpoint URL，而這個服務有兩頁要在 LINE 裡開。

    走 `liff.line.me/<id>/<path>` 的路徑寫法實測會落回 Endpoint URL 本身，而且
    LINE 會把結果當成「外部網站」而非 LIFF app（shareTargetPicker 就不能用）。
    query 參數才穩，所以兩條連結都必須是 `?p=` 的形式。
    """
    from conftest import build_payment_service
    import os

    os.environ["LIFF_ID"] = "1234567890-abcdefgh"
    try:
        link = build_payment_service().topup_link()
    finally:
        os.environ.pop("LIFF_ID", None)
    assert link.endswith("?p=start"), f"儲值連結要用 query 指定頁面，實際是 {link}"

    app_src = APP_PY.read_text(encoding="utf-8")
    assert "?p=share&no=" in app_src, "分享連結要用 query 指定頁面"
    # LIFF 進入點回 302 會讓 LINE 判定離開 LIFF app，根路由必須直接算繪
    assert "redirect(\"/gift\"" not in app_src, "根路由不可以轉址"


@pytest.mark.parametrize("page", ["pay.html", "gift_share.html"])
def test_liff_login_returns_to_the_page_it_started_on(page):
    """liff.login() 預設會把人送回 Endpoint URL，而這裡的 endpoint 是站台根目錄。

    不指定 redirectUri 的話，使用者登入完會落在根目錄（再被轉去 /gift），
    而不是他本來要去的付款頁或分享頁——按了按鈕卻跳到別頁就是這樣來的。
    """
    html = (TEMPLATES / page).read_text(encoding="utf-8")
    assert "liff.login({ redirectUri: window.location.href })" in html, \
        f"{page} 的 liff.login() 必須指定 redirectUri"


# --------------------------------------------------------- liff.state 的解析
#
# 從 LIFF URL 進來時，LINE 不會把附加的路徑與 query 接在 Endpoint URL 後面，而是
# 整包編碼進一個 liff.state 參數。只讀 request.args 的話什麼都拿不到，使用者會
# 落在根目錄的預設頁上——「按了沒反應」就是這麼來的。

@pytest.mark.parametrize("state,expect_page,expect_no", [
    ("?p=pay", "pay", None),
    ("?p=share&no=GY123", "share", "GY123"),
    # 早期版本把頁面放在路徑上，那些訊息還留在使用者的聊天室裡
    ("/pay", "pay", None),
    ("/gift/share?no=GY123", "share", "GY123"),
])
def test_liff_state_is_unpacked(state, expect_page, expect_no):
    from urllib.parse import quote, unquote, urlparse, parse_qs

    # 與 app._liff_params() 相同的解法，獨立驗證一次（app 匯入會連資料庫）
    decoded = unquote(quote(state))
    parsed = urlparse(decoded)
    query = parsed.query or (decoded.lstrip("?") if not parsed.path else "")
    params = {k: v[0] for k, v in parse_qs(query).items()}
    if "p" not in params and parsed.path:
        path = parsed.path.strip("/")
        params["p"] = {"pay": "pay", "gift/share": "share"}.get(path)

    assert params.get("p") == expect_page
    assert params.get("no") == expect_no


def test_root_route_unpacks_liff_state():
    """釘住根路由確實有走 _liff_params()，而不是直接讀 request.args"""
    app_src = APP_PY.read_text(encoding="utf-8")
    assert "liff.state" in app_src, "根路由必須認得 liff.state"
    assert "params = _liff_params()" in app_src


# ------------------------------------------------------- 一鍵領取：卡號不露臉
#
# 收禮的人按卡片上的按鈕就入帳，不必看到、不必輸入卡號。卡號降級成純內部識別。
# 「按那一下」省不掉——LINE 不讓我們推播給沒跟 bot 互動過的人，那一下就是互動。


def test_shared_card_offers_a_claim_button_not_a_code():
    """卡片上印卡號只會讓人想去哪裡輸入它；一顆按鈕就夠了"""
    html = (TEMPLATES / "gift_share.html").read_text(encoding="utf-8")

    assert "?p=claim&code=" in html, "卡片按鈕要導向一鍵領取頁"
    assert '"收下禮物"' in html
    # 沒有 LIFF 時仍要有退路，否則卡片會變成一張按不動的圖
    assert "line.me/R/oaMessage" in html, "沒有 LIFF 時要退回聊天室深連結"


def test_claim_endpoint_trusts_only_a_verified_id_token():
    """領取的去向由 LINE 驗過的 token 決定，不能由頁面自稱——否則誰都能
    把別人的禮物領到自己帳上。"""
    app_src = APP_PY.read_text(encoding="utf-8")
    claim = app_src[app_src.index("def gift_claim_redeem"):]
    claim = claim[:claim.index("@app.route", 1)] if "@app.route" in claim[1:] else claim

    assert "verify_id_token_claims" in claim
    assert "abort(401)" in claim
    assert 'data.get("user_id")' not in claim, "絕不能相信頁面自稱的 userId"


def test_claiming_notifies_the_chat():
    """領取是在 LIFF 頁裡完成的，聊天室不補一則的話關掉就什麼都沒了"""
    app_src = APP_PY.read_text(encoding="utf-8")
    assert "notify_gift_received" in app_src


def test_notification_reads_like_a_gift():
    from conftest import build_env
    env = build_env(with_member_feature=True, with_gift=True)
    feature = env.registry.get_feature_by_name("gift")

    feature.notify_gift_received("U" + "a" * 32, 600, 650)

    text = env.publisher.messages[-1]["text"]
    assert "禮物" in text and "600 點" in text and "650 點" in text


def test_ready_to_send_is_a_flex_card_with_a_send_button(monkeypatch):
    """付款完成通知是 Flex 卡片、附「送給朋友」按鈕，語氣不再是「還沒送出」"""
    from conftest import build_env
    monkeypatch.setenv("LIFF_ID", "1234567890-abcdefgh")
    env = build_env(with_member_feature=True, with_gift=True)
    feature = env.registry.get_feature_by_name("gift")

    feature.notify_gift_ready_to_send("U" + "a" * 32, 600, "GY123")

    msg = env.publisher.messages[-1]
    assert msg["type"] == "FlexSendMessage", "要是 Flex 卡片，不是純文字"
    alt = msg.get("alt_text", "")
    assert "禮物卡" in alt, "標記這是禮物卡"
    assert "謝謝您的購買" in alt, "跟自用購買完成一致的感謝"
    assert "還沒送出" not in alt, "不再嘮叨還沒送出"


# ---------------------------------------------- 送禮方從頭到尾看不到卡號
#
# 收禮改成點卡片一鍵領取，卡號降級為純內部識別。買家、收禮方的任何頁面上都
# 不該再出現卡號——螢幕上有一組卡號，只會引人去某處試著輸入它。


def test_giver_facing_pages_never_show_a_card_code():
    for name in ("start.html", "gift.html", "gift_done.html", "gift_share.html"):
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "禮物卡號" not in html, f"{name} 不該顯示卡號"


def test_done_page_is_a_transition_into_the_share_page():
    """完成頁降成純過場：自動跳進分享頁（那才是正式的付款完成頁）。使用者幾乎
    不會看到它，只有 iOS 擋掉自動跳時才需要點那顆保底按鈕。"""
    html = (TEMPLATES / "gift_done.html").read_text(encoding="utf-8")
    assert "window.location.href = link" in html, "要自動跳進分享頁"
    assert "?p=share&no=" in html


def test_share_page_is_the_payment_done_page():
    """分享頁現在是正式的付款完成頁，且沒有把換行寫成字面的 \n"""
    html = (TEMPLATES / "gift_share.html").read_text(encoding="utf-8")
    assert "付款完成" in html
    assert "選單…\\n" not in html, "換行不可以是字面的 \\n（要用 <br> 或真的換行）"


# ------------------------------------------- 已送出的卡：再開連結時提醒，別悶著再送


def test_share_page_marks_a_card_sent_after_sending():
    """送出成功要回報後端，卡才記得「送過了」"""
    html = (TEMPLATES / "gift_share.html").read_text(encoding="utf-8")
    assert "/gift/mark-sent" in html, "送出後要標記已送出"


def test_share_page_locks_when_reopened_after_sent():
    """從提醒連結再進來、卡已送過 → 不自動彈、按鈕鎖死。送給誰就是誰的，不能改送"""
    html = (TEMPLATES / "gift_share.html").read_text(encoding="utf-8")
    assert "data.sent" in html, "載入時要看卡是否已送出"
    assert "再送一次" not in html, "送出即定案，不提供再送"
    assert "已經送給朋友了" in html


def test_snapshot_reports_sent_state():
    """卡的快照要帶 sent 狀態（跨 session 邊界後仍讀得到）"""
    from src.services import gift_card_service as gc
    from src.models.gift_card import GiftCard
    from datetime import datetime, timezone

    card = GiftCard(code="ABCD1234", order_id="o", points=100, status='active')
    assert gc._snapshot(card).sent is False
    card.sent_at = datetime.now(timezone.utc)
    assert gc._snapshot(card).sent is True
