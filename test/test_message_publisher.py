"""MessagePublisher 的問候前綴：所有回覆／推播統一補「Hi 名字 😊」開頭。

E2E 測試走的是 conftest 的 FakePublisher，繞過這層；問候邏輯在真的
MessagePublisher 上，所以獨立在這裡驗。
"""
import pytest
from linebot.models import TextSendMessage, ImageSendMessage

from src.services.message_publisher import MessagePublisher

USER = "U-test"


class FakeLineBotApi:
    def __init__(self):
        self.replied = None
        self.pushed = None

    def reply_message(self, reply_token, messages):
        self.replied = messages

    def push_message(self, target_id, messages):
        self.pushed = messages


def make_publisher(name="阿嬤"):
    api = FakeLineBotApi()
    pub = MessagePublisher(api, name_resolver=lambda uid: name)
    return pub, api


def text_of(msg):
    return msg.text


class TestGreetingPrefix:
    def test_reply_single_text_gets_greeting(self):
        pub, api = make_publisher()
        pub.process_reply_message("tok", TextSendMessage(text="想做什麼呢？"), user_id=USER)
        assert api.replied.text == "Hi 阿嬤 😊\n\n想做什麼呢？"

    def test_push_text_gets_greeting(self):
        pub, api = make_publisher()
        pub.process_push_message(USER, TextSendMessage(text="做好了。"))
        assert api.pushed.text == "Hi 阿嬤 😊\n\n做好了。"

    def test_only_first_text_in_a_batch_is_greeted(self):
        pub, api = make_publisher()
        first, second = TextSendMessage(text="一"), TextSendMessage(text="二")
        pub.process_push_message(USER, [first, second])
        assert first.text == "Hi 阿嬤 😊\n\n一"
        assert second.text == "二", "後續泡泡不重複問候"

    def test_image_before_text_is_untouched_and_text_greeted(self):
        pub, api = make_publisher()
        img = ImageSendMessage(original_content_url="https://x/i.jpg",
                               preview_image_url="https://x/i.jpg")
        txt = TextSendMessage(text="做好了。")
        pub.process_push_message(USER, [img, txt])
        assert txt.text == "Hi 阿嬤 😊\n\n做好了。"
        assert img.original_content_url == "https://x/i.jpg", "圖片訊息不該被動到"

    def test_image_only_message_does_not_crash(self):
        pub, api = make_publisher()
        img = ImageSendMessage(original_content_url="https://x/i.jpg",
                               preview_image_url="https://x/i.jpg")
        pub.process_push_message(USER, img)
        assert api.pushed is img

    def test_falls_back_to_nameless_greeting(self):
        pub, api = make_publisher(name=None)
        pub.process_reply_message("tok", TextSendMessage(text="想做什麼呢？"), user_id=USER)
        assert api.replied.text == "Hi 😊\n\n想做什麼呢？"

    def test_no_resolver_still_greets_namelessly(self):
        api = FakeLineBotApi()
        pub = MessagePublisher(api)  # 沒有 name_resolver
        pub.process_reply_message("tok", TextSendMessage(text="哈囉"), user_id=USER)
        assert api.replied.text == "Hi 😊\n\n哈囉"
