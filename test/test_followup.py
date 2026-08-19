"""做完之後的「還要再做點什麼嗎」。

這個功能真正的風險不在按鈕，而在成品的生命週期：成品要留 30 天，而 state 裡
的暫存圖會在狀態轉換時被自動回收。交棒時若把成品當成暫存圖，用戶按下一步的
那一刻就會把自己的成品刪掉。下面 TestResultSurvives 的兩個測試就是釘住這件事。
"""
import pytest

from conftest import (
    ANIMATE_COST, COLORIZE_COST, EDIT_COST, RESULT_BYTES, USER, build_env,
)

DONE_BUTTON = "👌 不用了"
OFFER_BUTTONS = [
    f"🎬 做成影片（{ANIMATE_COST} 點）",
    f"🎨 再修一下（{EDIT_COST} 點）",
    DONE_BUTTON,
]


@pytest.fixture
def env():
    return build_env(points=200)


def colorize_a_photo(env):
    """跑完一次上色，停在成品剛推出去的那一刻"""
    env.send_image()
    env.send_text("幫照片上色")


class TestOffer:
    def test_result_is_followed_by_next_step_buttons(self, env):
        colorize_a_photo(env)

        pushed = [m for m in env.messages if m["kind"] == "push"]
        assert pushed[-2]["type"] == "ImageSendMessage", "成品自己先送"
        assert pushed[-1]["quick_reply"] == OFFER_BUTTONS, "按鈕另開一則，長輩才看得到"

    def test_buttons_state_the_cost(self, env):
        """點數寫在按鈕上，才不會按下去才發現要扣 25 點"""
        colorize_a_photo(env)

        assert f"{ANIMATE_COST} 點" in env.quick_reply[0]

    def test_offer_remembers_the_result(self, env):
        colorize_a_photo(env)

        assert env.state_is("followup", "offered")
        assert env.state["data"]["result_url"], "要記得成品的網址，用戶才不必重傳"

    def test_video_result_gets_no_offer(self, env):
        """影片餵不回圖片模型，所以不給後續選項"""
        env.send_text("照片動起來")
        env.send_image()
        env.reset()

        env.send_text("確定開始")

        assert env.state is None, "沒有後續選項時照原本的方式收尾"
        assert env.quick_reply == []

    def test_group_chat_gets_no_offer(self, env):
        """follow-up 狀態掛在個人身上，群組裡誰接著講話都會踩到它"""
        env.send_text("修復老照片", source_type="group")
        env.send_image(source_type="group")

        assert env.state is None
        assert env.quick_reply == []

    def test_failed_delivery_gets_no_offer(self, env):
        """沒送達就退點，這時不該留下「還要再做點什麼嗎」的狀態"""
        env.publisher.push_fails = True

        colorize_a_photo(env)

        assert env.member.refunds, "沒送達要退點"
        assert env.state is None


class TestHandoff:
    def test_make_a_video_from_the_result(self, env):
        colorize_a_photo(env)
        env.reset()

        env.send_text("做成影片")

        assert env.state_is("animate", "waiting_confirm"), "交棒後由 animate 接手確認"
        assert f"{ANIMATE_COST} 點" in env.last_text

    def test_handoff_feeds_the_result_not_the_original(self, env):
        """接著做的是「剛上好色的那張」，不是用戶原本的黑白照"""
        colorize_a_photo(env)

        env.send_text("做成影片")

        assert list(env.stashed_objects.values()) == [RESULT_BYTES]

    def test_edit_the_result_again(self, env):
        colorize_a_photo(env)
        env.reset()

        env.send_text("再修一下")

        assert env.state_is("edit", "waiting_description")

    def test_done_ends_the_conversation_cleanly(self, env):
        colorize_a_photo(env)
        env.reset()

        env.send_text("不用了")

        assert env.state is None
        assert env.member.deductions == [{"amount": COLORIZE_COST, "feature": "colorize",
                                          "description": "彩色化圖片"}], "按了不用了不該再扣點"

    def test_no_charge_until_the_next_flow_is_confirmed(self, env):
        colorize_a_photo(env)
        before = env.member.points

        env.send_text("做成影片")

        assert env.member.points == before, "交棒只是進到確認階段，還不扣點"

    def test_expired_result_asks_for_the_photo_again(self, env):
        """成品過了保存期就取不回來，要講清楚而不是讓按鈕沒反應"""
        colorize_a_photo(env)
        env.archive.download_fails = True
        env.reset()

        env.send_text("做成影片")

        assert "再傳一次" in env.last_text
        assert env.state is None

    def test_insufficient_points_leaves_nothing_behind(self, env):
        """點數只夠上色、不夠做影片：擋下之後不能留下孤兒暫存圖"""
        env = build_env(points=COLORIZE_COST)
        colorize_a_photo(env)
        env.reset()

        env.send_text("做成影片")

        assert "點數不夠" in env.last_text
        assert env.state is None
        assert env.stashed_objects == {}, "被擋下的交棒要把複製出來的暫存圖收掉"


class TestResultSurvives:
    """成品要活過 30 天，不能被狀態轉換順手回收掉"""

    def test_result_survives_the_handoff(self, env):
        colorize_a_photo(env)
        archived_before = dict(env.archived_objects)

        env.send_text("做成影片")

        assert env.archived_objects == archived_before, "交棒不得動到 results/ 裡的成品"

    def test_result_survives_starting_something_else(self, env):
        """晾著不理、直接開始下一件事，成品同樣不能被清掉"""
        colorize_a_photo(env)
        archived_before = dict(env.archived_objects)

        env.send_text("圖片編輯")

        assert env.archived_objects == archived_before


class TestRoutingIsNotHijacked:
    """follow-up 狀態會留在對話裡，不能因此吃掉用戶想對別的功能講的話"""

    def test_trigger_command_still_works(self, env):
        colorize_a_photo(env)
        env.reset()

        env.send_text("修復老照片")

        assert env.state_is("colorize", "waiting")

    def test_global_command_still_works(self):
        env = build_env(points=200, with_member_feature=True)
        colorize_a_photo(env)
        env.reset()

        env.send_text("點數")

        assert "點" in env.last_text

    def test_a_new_photo_starts_a_new_conversation(self, env):
        colorize_a_photo(env)
        env.reset()

        env.send_image()

        assert env.state_is("photo_intent", "waiting_choice")
