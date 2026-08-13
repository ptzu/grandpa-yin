"""Offline test harness: in-memory fakes for every external system.

Nothing here touches PostgreSQL, LINE, Replicate or Supabase — tests drive the
real FeatureRegistry and the real features, with the boundaries faked out. This
is also the single place that knows how features are wired, so an architectural
change to the wiring only edits `build_env()` below.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.core.settings import get_model_config
from src.services import billing as billing_module
from src.services.billing import BillingService
from src.features.context import FeatureContext
from src.features.feature_registry import FeatureRegistry
from src.features.menu_feature import MenuFeature
from src.features.colorize_feature import ColorizeFeature
from src.features.edit_feature import EditFeature
from src.features.member_feature import MemberFeature
from src.features.photo_intent_feature import PhotoIntentFeature

USER = "U-test-user"
FAKE_OUTPUT_URL = "https://example.test/output.jpg"
IMAGE_BYTES = b"\xff\xd8fake-jpeg"

# Read from the shipped config/settings.yml, so the suite asserts against whatever
# is actually configured — and fails loudly if that file stops being valid.
COLORIZE_CONFIG = get_model_config("colorize")
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

    def process_reply_message(self, reply_token, message, user_id=None, event=None):
        self._record("reply", message)

    def reply_text(self, reply_token, text, user_id=None, event=None):
        from linebot.models import TextSendMessage
        self._record("reply", TextSendMessage(text=text))

    def process_push_message(self, user_id, message, event=None):
        self._record("push", message)

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

    def state_is(self, feature, state):
        current = self.state
        return bool(current and current["feature"] == feature and current["state"] == state)


def build_env(points=100, with_member_feature=False, replicate_fails_with=None):
    """Assemble a registry the same way app.py does (photo_intent registered last).

    BillingService is the real one — only the systems at the edges are faked.
    """
    publisher = FakePublisher()
    state_manager = FakeStateManager()
    member_service = FakeMemberService(points)
    storage = FakeStorage()
    replicate = FakeReplicateClient(fail_with=replicate_fails_with)

    ctx = FeatureContext(
        line=FakeLineClient(),
        publisher=publisher,
        state_manager=state_manager,
        billing=BillingService(member_service, publisher),
        replicate=replicate,
        member_service=member_service,
        storage_service=storage,
    )

    registry = FeatureRegistry(state_manager)
    registry.register(MenuFeature(ctx))
    registry.register(ColorizeFeature(ctx))
    registry.register(EditFeature(ctx))
    if with_member_feature:
        registry.register(MemberFeature(ctx))
    registry.register(PhotoIntentFeature(ctx))

    return Env(registry, publisher, state_manager, member_service, storage, replicate)


# ------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def offline_externals(monkeypatch):
    """Run billed background tasks synchronously instead of on the thread pool.

    Everything else reaching outside the process already goes through a fake
    injected via FeatureContext, so this is the only patch the suite needs.
    """
    monkeypatch.setattr(billing_module, "submit_image_task",
                        lambda task: (task(), True)[1])


@pytest.fixture
def env():
    return build_env()


@pytest.fixture
def make_env():
    return build_env
