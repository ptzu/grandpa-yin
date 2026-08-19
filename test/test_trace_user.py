"""The ledger seam scripts/trace_user.py reads through.

The script used to query Altide's public.transactions directly, so it only
worked in platform mode. These tests pin the per-mode ledger wiring that
replaced it — no DB connection needed, only the model metadata.
"""
from scripts.trace_user import ledger_owned_by
from src.models.transaction import Transaction
from src.models.wallet_transaction import WalletTransaction
from src.services.account_backend import (
    PlatformAccountBackend,
    StandaloneAccountBackend,
)


class FakeAccount:
    id = "acc-1"


def test_platform_ledger_is_altide_transactions():
    assert PlatformAccountBackend.ledger_model is Transaction


def test_standalone_ledger_is_own_wallet():
    assert StandaloneAccountBackend.ledger_model is WalletTransaction


def test_owner_column_platform():
    assert "account_id" in str(ledger_owned_by(Transaction, FakeAccount()))


def test_owner_column_standalone():
    """wallet_transactions has no account_id — it must fall back to subject_id"""
    assert "subject_id" in str(ledger_owned_by(WalletTransaction, FakeAccount()))
