"""Find a member by display name or LINE userId — the lookup behind the
operator scripts (`scripts/add_points.py`, `scripts/trace_user.py`).

Identity resolution goes through AccountBackend instead of querying the
identity tables directly, so the same lookup works in both platform and
standalone mode. Only GrandpaYinProfile is queried here, and that table is
owned by this product in both modes.

Read-only: nothing in here mutates points. Callers decide what to do with the
match.
"""
from collections import namedtuple

from src.models.grandpa_yin_profile import GrandpaYinProfile
from src.services.account_backend import get_account_backend

# subject: the backend's account/subject row (has .id / .points_balance)
# profile: GrandpaYinProfile, or None when the member has no profile row yet
# line_uid: the LINE userId the member is reachable by
MemberMatch = namedtuple("MemberMatch", "subject profile line_uid")


def looks_like_line_uid(text):
    """LINE userId format: leading "U" + 32 hex chars."""
    if len(text) != 33 or not text.startswith('U'):
        return False
    try:
        int(text[1:], 16)
        return True
    except ValueError:
        return False


def display_name_of(match):
    """Display name for a match, falling back the same way MemberService does."""
    return (match.profile.display_name if match.profile else None) or '使用者'


class MemberDirectory:
    """Look members up for the operator tooling."""

    def __init__(self, backend=None):
        self._backend = backend or get_account_backend()

    def find_by_uid(self, session, line_uid):
        """Exact lookup. Returns a MemberMatch, or None when not linked."""
        subject = self._backend.resolve(session, line_uid)
        if not subject:
            return None
        profile = (
            session.query(GrandpaYinProfile)
            .filter_by(account_id=subject.id)
            .first()
        )
        return MemberMatch(subject, profile, line_uid)

    def find_by_name(self, session, name):
        """Substring, case-insensitive lookup on display name.

        Returns every match, so the caller can ask which one was meant rather
        than guessing — picking the wrong person here means crediting points to
        a stranger.
        """
        profiles = (
            session.query(GrandpaYinProfile)
            .filter(GrandpaYinProfile.display_name.ilike(f"%{name}%"))
            .order_by(GrandpaYinProfile.display_name)
            .all()
        )
        uid_map = self._backend.provider_uid_map(
            session, [p.account_id for p in profiles]
        )

        matches = []
        for profile in profiles:
            line_uid = uid_map.get(profile.account_id)
            if not line_uid:
                # A profile with no LINE identity cannot be credited or notified.
                continue
            subject = self._backend.resolve(session, line_uid)
            if subject:
                matches.append(MemberMatch(subject, profile, line_uid))
        return matches

    def search(self, session, identifier):
        """Name or userId, whichever the operator typed. Always a list."""
        if looks_like_line_uid(identifier):
            match = self.find_by_uid(session, identifier)
            return [match] if match else []
        return self.find_by_name(session, identifier)
