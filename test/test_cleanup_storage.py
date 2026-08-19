"""scripts/cleanup_storage.py：時間解析，以及「暫存圖 vs 成品」的分區判定。

分區那部分是這支腳本最危險的地方：把 results/ 當成暫存圖掃，等於每天早上把
用戶的成品全刪光，而且沒有任何人會收到錯誤。
"""
from datetime import datetime, timedelta, timezone

import pytest

from scripts.cleanup_storage import (
    collect_expired_results,
    collect_objects,
    parse_timestamp,
)
from src.services.result_archive import RESULT_PREFIX

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


def iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class FakeListingStorage:
    """只實作 cleanup 腳本用得到的兩個列表 API"""

    def __init__(self, tree):
        # tree: {prefix: [(name, created_at), ...]}，"" 代表根層
        self.tree = tree

    def list_folders(self, prefix=""):
        return [p for p in self.tree if p and p != prefix]

    def list_objects(self, prefix=""):
        return [
            {"key": f"{prefix}/{name}" if prefix else name, "created_at": created_at}
            for name, created_at in self.tree.get(prefix, [])
        ]


@pytest.fixture
def storage():
    return FakeListingStorage({
        "": [],
        "edit": [("old.jpg", iso(3)), ("fresh.jpg", iso(0))],
        "animate": [("stale.jpg", iso(9))],
        RESULT_PREFIX: [("keep.jpg", iso(5)), ("expired.mp4", iso(40))],
    })


class TestPartitioning:
    def test_results_are_not_swept_as_temp_images(self, storage):
        """成品沒有任何 bot_session 引用，混進暫存圖那區就會在 24 小時後全滅"""
        keys = [item["key"] for item in collect_objects(storage)]

        assert not any(k.startswith(f"{RESULT_PREFIX}/") for k in keys), \
            f"results/ 不該出現在暫存圖清單：{keys}"
        assert "edit/old.jpg" in keys and "animate/stale.jpg" in keys

    def test_only_expired_results_are_collected(self, storage):
        expired, unknown = collect_expired_results(storage, days=31)

        assert [key for key, _ in expired] == [f"{RESULT_PREFIX}/expired.mp4"]
        assert unknown == 0

    def test_retention_boundary_keeps_everything_recent(self, storage):
        """保留期拉長就不該刪任何東西——用戶還看得到的絕不能刪"""
        expired, _ = collect_expired_results(storage, days=90)

        assert expired == []

    def test_unreadable_timestamps_are_kept(self):
        storage = FakeListingStorage({RESULT_PREFIX: [("mystery.jpg", "not-a-date")]})

        expired, unknown = collect_expired_results(storage, days=31)

        assert expired == [], "讀不到時間就保守不刪"
        assert unknown == 1
