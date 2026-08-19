"""Offline test harness: in-memory fakes for every external system.

Nothing here touches PostgreSQL, LINE, Replicate or Supabase — tests drive the
real FeatureRegistry and the real features, with the boundaries faked out. This
is also the single place that knows how features are wired, so an architectural
change to the wiring only edits `build_env()` below.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.core.settings import get_model_config
from src.services.preview_store import LocalPreviewStore, set_public_base_url
from src.services.result_archive import RESULT_PREFIX, ResultArchive
from src.services import billing as billing_module
from src.services.billing import BillingService
from src.features.context import FeatureContext
from src.features.feature_registry import FeatureRegistry
from src.features.menu_feature import MenuFeature
from src.features.colorize_feature import ColorizeFeature
from src.features.animate_feature import AnimateFeature
from src.features.edit_feature import EditFeature
from src.features.member_feature import MemberFeature
from src.features.followup_feature import FollowUpFeature
from src.features.photo_intent_feature import PhotoIntentFeature
from src.core.settings import _parse_payments
from src.services.payment_service import PaymentService

USER = "U-test-user"
FAKE_OUTPUT_URL = "https://example.test/output.jpg"
IMAGE_BYTES = b"\xff\xd8fake-jpeg"
RESULT_BYTES = b"\xff\xd8fake-result"

# Read from the shipped config/settings.yml, so the suite asserts against whatever
# is actually configured — and fails loudly if that file stops being valid.
COLORIZE_CONFIG = get_model_config("colorize")
ANIMATE_CONFIG = get_model_config("animate")
ANIMATE_COST = ANIMATE_CONFIG.cost
EDIT_CONFIG = get_model_config("edit")
COLORIZE_COST = COLORIZE_CONFIG.cost
EDIT_COST = EDIT_CONFIG.cost


# ---------------------------------------------------------------- fakes


class FakeStateManager:
    """In-memory UserStateManager, matching the read/write semantics of bot_sessions"""

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
        # 設 True 模擬 LINE 退件（例如網址不合規），用來驗證推送失敗會退點
        self.push_fails = False

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
            # 媒體訊息指向哪裡，決定用戶下個月回頭看還在不在
            "media_url": getattr(message, "original_content_url", None),
            "preview_url": getattr(message, "preview_image_url", None),
        })

    def process_reply_message(self, reply_token, message, user_id=None, event=None):
        self._record("reply", message)

    def reply_text(self, reply_token, text, user_id=None, event=None):
        from linebot.models import TextSendMessage
        self._record("reply", TextSendMessage(text=text))

    def process_push_message(self, user_id, messages, event=None):
        # 真實的 push_message 收單則或一串；成品 + 後續選項就是一串送出
        batch = messages if isinstance(messages, list) else [messages]
        for message in batch:
            self._record("push", message)
        # 只讓「結果訊息」失敗，後續的道歉文字仍送得出去（貼近真實情況）
        if self.push_fails and any(type(m).__name__ != "TextSendMessage" for m in batch):
            return False
        return True

    def reset(self):
        self.messages = []

    @property
    def last(self):
        return self.messages[-1] if self.messages else None

    def texts(self):
        return [m["text"] for m in self.messages if m["text"]]


class FakeLineClient:
    """Stands in for services.line_client.LineClient (LINE receive side)"""

    def __init__(self):
        self.loading_animations = []

    def download_message_content(self, message_id):
        return IMAGE_BYTES

    def get_display_name(self, user_id):
        return "阿嬤"

    def start_loading_animation(self, user_id, seconds=30):
        self.loading_animations.append((user_id, seconds))


class FakeReplicateClient:
    """Stands in for services.replicate_client.ReplicateClient — never leaves the process.

    `fail_with` makes the model call raise, to exercise the refund path.
    """

    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def run(self, model, input_dict):
        self.calls.append({"model": model, "input": input_dict})
        if self.fail_with:
            raise self.fail_with
        return FAKE_OUTPUT_URL

    @staticmethod
    def image_to_data_url(image_bytes):
        return "data:image/jpeg;base64,ZmFrZQ=="


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.signed = []
        self.uploads = []
        self._counter = 0

    def is_configured(self):
        return True

    def upload_object(self, data, prefix, extension, content_type):
        self._counter += 1
        key = f"{prefix}/{self._counter}.{extension}"
        self.objects[key] = data
        self.uploads.append((key, content_type))
        return key

    def upload_image(self, image_bytes, prefix="tmp"):
        return self.upload_object(image_bytes, prefix, "jpg", "image/jpeg")

    def download_image(self, key):
        return self.objects[key]

    def delete_image(self, key):
        self.objects.pop(key, None)
        self.deleted.append(key)

    def create_signed_url(self, key, expires_in=86400):
        if key not in self.objects:
            raise KeyError(key)
        self.signed.append((key, expires_in))
        return f"https://storage.test/signed/{key}?token=fake"


class FakeResultArchive(ResultArchive):
    """The real archive, with only the download faked out.

    Subclassing rather than re-implementing keeps the interesting parts under
    test: type resolution, the upload, and the signed URL's lifetime.
    """

    def __init__(self, storage):
        super().__init__(storage)
        self.downloaded = []
        # 設 True 模擬抓不到成品，用來驗證會退回模型的原始網址
        self.download_fails = False

    def _download(self, url):
        if self.download_fails:
            raise RuntimeError("fake download failure")
        self.downloaded.append(url)
        return RESULT_BYTES, "image/jpeg"


class FakeMemberService:
    def __init__(self, points=100):
        self.points = points
        self.deductions = []
        self.refunds = []

    def _member(self):
        # Same shape as MemberService._member_dict()
        return {
            "user_id": USER,
            "display_name": "阿嬤",
            "points": self.points,
            "status": "normal",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": None,
        }

    def get_or_create_member(self, user_id, display_name=None):
        return self._member()

    def get_member_info(self, user_id):
        return self._member()

    def get_point_history(self, user_id, limit=10):
        return []

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


class Env:
    """A fully wired bot with every external system faked out."""

    def __init__(self, registry, publisher, state_manager, member_service, storage, replicate):
        self.registry = registry
        self.publisher = publisher
        self.state_manager = state_manager
        self.member = member_service
        self.storage = storage
        self.replicate = replicate

    # --- driving the bot ---

    def send_text(self, text, source_type="user"):
        return self.registry.route_text_message(text_event(text, source_type=source_type))

    def send_image(self, source_type="user"):
        return self.registry.route_image_message(image_event(source_type=source_type))

    # --- observing the bot ---

    @property
    def state(self):
        return self.state_manager.states.get(USER)

    @property
    def stashed_objects(self):
        """Storage 裡的暫存圖（不含刻意保留 30 天的成品）"""
        return {k: v for k, v in self.storage.objects.items()
                if not k.startswith(f"{RESULT_PREFIX}/")}

    @property
    def archived_objects(self):
        """Storage 裡保留給用戶的成品"""
        return {k: v for k, v in self.storage.objects.items()
                if k.startswith(f"{RESULT_PREFIX}/")}

    @property
    def messages(self):
        return self.publisher.messages

    @property
    def last(self):
        return self.publisher.last

    @property
    def last_text(self):
        return (self.publisher.last or {}).get("text") or ""

    @property
    def quick_reply(self):
        return (self.publisher.last or {}).get("quick_reply") or []

    def reset(self):
        self.publisher.reset()

    def pushed_image(self):
        return any(m["type"] == "ImageSendMessage" for m in self.publisher.messages)

    def pushed_media(self):
        """最後一則帶媒體網址的訊息（圖片或影片）；沒有就回 None"""
        media = [m for m in self.publisher.messages if m["media_url"]]
        return media[-1] if media else None

    def state_is(self, feature, state):
        current = self.state
        return bool(current and current["feature"] == feature and current["state"] == state)


TEST_PACKAGES = {
    "provider": "ecpay",
    "packages": [
        {"id": "s", "points": 100, "price_twd": 100},
        {"id": "m", "points": 300, "price_twd": 250, "label": "300 點（較划算）"},
    ],
}


class FakeGateway:
    """Stands in for ECPayClient; top-up tests never reach the gateway."""

    api_url = "https://gateway.test/checkout"

    def checkout_params(self, **kwargs):
        return {"MerchantTradeNo": kwargs["merchant_trade_no"]}

    def verify(self, payload):
        return True


def build_payment_service():
    """A real PaymentService with faked edges. Callers still need LIFF_ID set
    for `topup_link()` to return anything — that is the behaviour under test."""
    settings = _parse_payments(TEST_PACKAGES)
    return PaymentService(ecpay=FakeGateway(), backend=object(),
                          settings_provider=lambda: settings)


def build_env(points=100, with_member_feature=False, replicate_fails_with=None,
              with_payments=False):
    """Assemble a registry the same way app.py does (photo_intent registered last).

    BillingService is the real one — only the systems at the edges are faked.
    """
    publisher = FakePublisher()
    state_manager = FakeStateManager()
    member_service = FakeMemberService(points)
    storage = FakeStorage()
    replicate = FakeReplicateClient(fail_with=replicate_fails_with)
    preview_store = LocalPreviewStore(directory=tempfile.mkdtemp(prefix='gy-preview-test-'))
    result_archive = FakeResultArchive(storage)

    ctx = FeatureContext(
        line=FakeLineClient(),
        publisher=publisher,
        state_manager=state_manager,
        billing=BillingService(member_service, publisher),
        replicate=replicate,
        member_service=member_service,
        storage_service=storage,
        preview_store=preview_store,
        result_archive=result_archive,
        payment_service=build_payment_service() if with_payments else None,
    )

    registry = FeatureRegistry(state_manager)
    registry.register(MenuFeature(ctx))
    registry.register(ColorizeFeature(ctx))
    registry.register(EditFeature(ctx))
    registry.register(AnimateFeature(ctx))
    registry.register(FollowUpFeature(ctx))
    if with_member_feature:
        registry.register(MemberFeature(ctx))
    registry.register(PhotoIntentFeature(ctx))

    env = Env(registry, publisher, state_manager, member_service, storage, replicate)
    env.preview_store = preview_store
    env.archive = result_archive
    return env


# ------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def offline_externals(monkeypatch):
    """Run billed background tasks synchronously instead of on the thread pool.

    Everything else reaching outside the process already goes through a fake
    injected via FeatureContext, so this is the only patch the suite needs.
    """
    monkeypatch.setattr(billing_module, "submit_image_task",
                        lambda task: (task(), True)[1])
    # 公開網址是 ContextVar，會殘留到下一個測試——每次歸零，避免順序相依
    set_public_base_url(None)


@pytest.fixture
def env():
    return build_env()


@pytest.fixture
def make_env():
    return build_env
