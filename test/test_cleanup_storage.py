"""Timestamp parsing used by scripts/cleanup_storage.py to decide what is stale."""
import pytest

from scripts.cleanup_storage import parse_timestamp

PARSEABLE = [
    "2026-08-09T12:34:56Z",
    "2026-08-09T12:34:56.789Z",
    # Supabase 常回傳超過微秒精度的位數，3.9 的 fromisoformat 吃不下
    "2026-08-09T12:34:56.1234567Z",
    "2026-08-09T12:34:56+00:00",
    "2026-08-09T12:34:56",
]


@pytest.mark.parametrize("value", PARSEABLE)
def test_parses_supabase_timestamps(value):
    parsed = parse_timestamp(value)
    assert parsed is not None
    assert parsed.tzinfo is not None, "必須帶時區資訊才能安全比較"


def test_none_returns_none():
    assert parse_timestamp(None) is None


def test_garbage_returns_none():
    """壞字串回傳 None，讓呼叫端保守地不刪"""
    assert parse_timestamp("not-a-date") is None
