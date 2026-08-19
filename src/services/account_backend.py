"""Account backend port + adapters — the seam that lets grandpa_yin either

  * run standalone (own identity + wallet in grandpa_yin.*), or
  * integrate into Altide (shared public.accounts / linked_identities /
    transactions),

without the business logic (member_service, user_state_manager) knowing which.

A "subject" is any object exposing `.id` / `.points_balance` / `.created_at`
and attached to the passed SQLAlchemy session, so balance mutations and row
locks work uniformly. Both adapters return such an object (Account or Subject).

The mode is chosen once from DEPLOY_MODE (default "platform" so existing
production behaviour is unchanged).
"""
import os
from abc import ABC, abstractmethod

from src.core.app_logger import get_logger
from src.models.account import Account
from src.models.linked_identity import LinkedIdentity
from src.models.transaction import Transaction
from src.models.subject import Subject
from src.models.wallet_transaction import WalletTransaction

logger = get_logger("account_backend")

LINE_PROVIDER = 'line'


class AccountBackend(ABC):
    """Port isolating grandpa_yin from its identity + points source."""

    # --- identity -----------------------------------------------------------
    @abstractmethod
    def resolve(self, session, line_uid, *, for_update=False):
        """LINE UID -> subject, or None if not linked."""

    @abstractmethod
    def get_or_create(self, session, line_uid):
        """LINE UID -> subject, creating a shadow subject/account if needed."""

    @abstractmethod
    def provider_uid_map(self, session, subject_ids):
        """{subject_id: line_uid} for the given subject ids (reverse lookup)."""

    # --- ledger (per-mode table) -------------------------------------------
    # The model backing this mode's ledger. Exposed so read-only tooling can
    # query it without knowing which mode is active.
    ledger_model = None

    @abstractmethod
    def _new_ledger_row(self, subject_id, *, amount, service, balance_after, description):
        """Build (not persist) a ledger row for this mode's ledger table."""

    @abstractmethod
    def has_transaction(self, session, subject, *, service, description):
        """True if a ledger row already exists for (subject, service, description).
        Used for idempotent signup-bonus granting."""

    @abstractmethod
    def history_rows(self, session, subject, *, limit):
        """Most-recent ledger rows for a subject (each row exposes
        id/amount/balance_after/description/created_at)."""

    # --- wallet (shared: balance lives on the subject in both modes) --------
    def credit(self, session, subject, points, *, service, description):
        """Add points and append a ledger row. Returns the new balance."""
        subject.points_balance += points
        session.add(self._new_ledger_row(
            subject.id, amount=points, service=service,
            balance_after=subject.points_balance, description=description))
        return subject.points_balance

    def debit(self, session, subject, points, *, service, description):
        """Subtract points and append a ledger row. Caller must have checked
        sufficiency. Returns the new balance."""
        subject.points_balance -= points
        session.add(self._new_ledger_row(
            subject.id, amount=-points, service=service,
            balance_after=subject.points_balance, description=description))
        return subject.points_balance


class PlatformAccountBackend(AccountBackend):
    """Backed by Altide's shared public.* tables."""

    ledger_model = Transaction

    def resolve(self, session, line_uid, *, for_update=False):
        identity = (
            session.query(LinkedIdentity)
            .filter_by(provider=LINE_PROVIDER, provider_uid=line_uid)
            .first()
        )
        if not identity:
            return None
        q = session.query(Account).filter_by(id=identity.account_id)
        if for_update:
            q = q.with_for_update()
        return q.first()

    def get_or_create(self, session, line_uid):
        account = self.resolve(session, line_uid)
        if account:
            return account
        account = Account(points_balance=0, is_admin=False)
        session.add(account)
        session.flush()  # obtain account.id
        session.add(LinkedIdentity(
            account_id=account.id,
            provider=LINE_PROVIDER,
            provider_uid=line_uid,
        ))
        return account

    def provider_uid_map(self, session, subject_ids):
        if not subject_ids:
            return {}
        rows = (
            session.query(LinkedIdentity.account_id, LinkedIdentity.provider_uid)
            .filter(LinkedIdentity.provider == LINE_PROVIDER)
            .filter(LinkedIdentity.account_id.in_(list(subject_ids)))
            .all()
        )
        return {account_id: provider_uid for account_id, provider_uid in rows}

    def _new_ledger_row(self, subject_id, *, amount, service, balance_after, description):
        return Transaction(
            account_id=subject_id, amount=amount, service=service,
            balance_after=balance_after, description=description,
        )

    def has_transaction(self, session, subject, *, service, description):
        return (
            session.query(Transaction)
            .filter_by(account_id=subject.id, service=service, description=description)
            .first()
        ) is not None

    def history_rows(self, session, subject, *, limit):
        return (
            session.query(Transaction)
            .filter_by(account_id=subject.id)
            .order_by(Transaction.created_at.desc())
            .limit(limit)
            .all()
        )


class StandaloneAccountBackend(AccountBackend):
    """Backed by grandpa_yin-owned subjects / wallet_transactions. No Altide."""

    ledger_model = WalletTransaction

    def resolve(self, session, line_uid, *, for_update=False):
        q = session.query(Subject).filter_by(provider=LINE_PROVIDER, provider_uid=line_uid)
        if for_update:
            q = q.with_for_update()
        return q.first()

    def get_or_create(self, session, line_uid):
        subject = self.resolve(session, line_uid)
        if subject:
            return subject
        subject = Subject(
            provider=LINE_PROVIDER, provider_uid=line_uid,
            points_balance=0, is_admin=False,
        )
        session.add(subject)
        session.flush()  # obtain subject.id
        return subject

    def provider_uid_map(self, session, subject_ids):
        if not subject_ids:
            return {}
        rows = (
            session.query(Subject.id, Subject.provider_uid)
            .filter(Subject.id.in_(list(subject_ids)))
            .all()
        )
        return {subject_id: provider_uid for subject_id, provider_uid in rows}

    def _new_ledger_row(self, subject_id, *, amount, service, balance_after, description):
        return WalletTransaction(
            subject_id=subject_id, amount=amount, service=service,
            balance_after=balance_after, description=description,
        )

    def has_transaction(self, session, subject, *, service, description):
        return (
            session.query(WalletTransaction)
            .filter_by(subject_id=subject.id, service=service, description=description)
            .first()
        ) is not None

    def history_rows(self, session, subject, *, limit):
        return (
            session.query(WalletTransaction)
            .filter_by(subject_id=subject.id)
            .order_by(WalletTransaction.created_at.desc())
            .limit(limit)
            .all()
        )


_BACKENDS = {
    'platform': PlatformAccountBackend,
    'standalone': StandaloneAccountBackend,
}

_backend_singleton = None


def get_account_backend():
    """Return the process-wide AccountBackend selected by DEPLOY_MODE.

    Default "platform" keeps current production behaviour when the variable is
    unset. Set DEPLOY_MODE=standalone to run without Altide.
    """
    global _backend_singleton
    if _backend_singleton is None:
        mode = os.getenv("DEPLOY_MODE", "platform").strip().lower()
        backend_cls = _BACKENDS.get(mode)
        if backend_cls is None:
            raise RuntimeError(
                f"未知的 DEPLOY_MODE={mode!r}，可用值：{sorted(_BACKENDS)}"
            )
        _backend_singleton = backend_cls()
        logger.info(f"AccountBackend 模式：{mode}")
    return _backend_singleton
