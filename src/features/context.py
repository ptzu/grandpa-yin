"""The bundle of collaborators every feature is constructed with.

Features used to take five positional arguments, so giving one feature a new
dependency meant editing BaseFeature, every subclass constructor and every call
site in app.py. Now a new collaborator is one field here plus one line in app.py's
wiring; features that don't need it are untouched.

Lives in features/ rather than core/ because it names service types, and
`core` must not depend on `services` (see the dependency direction in README).
"""
from dataclasses import dataclass
from typing import Optional

from src.services.billing import BillingService
from src.services.line_client import LineClient
from src.services.message_publisher import MessagePublisher
from src.services.replicate_client import ReplicateClient
from src.services.user_state_manager import UserStateManager


@dataclass(frozen=True)
class FeatureContext:
    """Everything a feature is allowed to reach the outside world through.

    `member_service` and `storage_service` are optional: the bot still boots
    (in a degraded mode) when the database or Supabase Storage is unconfigured,
    and features check for them rather than assuming they exist.
    """

    line: LineClient
    publisher: MessagePublisher
    state_manager: UserStateManager
    billing: BillingService
    replicate: ReplicateClient
    member_service: Optional[object] = None
    storage_service: Optional[object] = None
    # Serves thumbnails from the app itself when Storage isn't configured
    preview_store: Optional[object] = None
    # Keeps finished results reachable for 30 days; None = deliver the model URL as-is
    result_archive: Optional[object] = None
    # Top-up; None (or disabled) means the bot simply doesn't offer it
    payment_service: Optional[object] = None
