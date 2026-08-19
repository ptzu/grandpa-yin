"""Member lookup + the operator prompts behind scripts/add_points.py.

Offline like the rest of the suite: the account backend and the SQLAlchemy
session are faked, so these tests cover the wiring (who gets matched, what the
prompts do with the answer) rather than SQL semantics.
"""
import pytest

from scripts.add_points import _fit, confirm, select_match
from src.services.member_directory import (
    MemberDirectory,
    MemberMatch,
    display_name_of,
    looks_like_line_uid,
)
from src.models.grandpa_yin_profile import GrandpaYinProfile

UID_A = "U" + "a" * 32
UID_B = "U" + "b" * 32


class FakeSubject:
    def __init__(self, subject_id, points_balance):
        self.id = subject_id
        self.points_balance = points_balance


class FakeProfile:
    def __init__(self, account_id, display_name):
        self.account_id = account_id
        self.display_name = display_name


class FakeQuery:
    """Chainable no-op query: filtering is SQL's job, not this test's."""

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def filter_by(self, **kwargs):
        matched = [r for r in self._rows
                   if all(getattr(r, k) == v for k, v in kwargs.items())]
        return FakeQuery(matched)

    def order_by(self, *args):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    def __init__(self, profiles):
        self._profiles = profiles

    def query(self, model):
        assert model is GrandpaYinProfile, f"未預期的查詢對象: {model}"
        return FakeQuery(self._profiles)


class FakeBackend:
    """Maps LINE UID -> subject, mirroring AccountBackend's two lookup methods."""

    def __init__(self, subjects_by_uid):
        self._subjects_by_uid = subjects_by_uid

    def resolve(self, session, line_uid, *, for_update=False):
        return self._subjects_by_uid.get(line_uid)

    def provider_uid_map(self, session, subject_ids):
        wanted = set(subject_ids)
        return {s.id: uid for uid, s in self._subjects_by_uid.items()
                if s.id in wanted}


@pytest.fixture
def directory_env():
    """Two linked members plus one profile with no LINE identity."""
    subjects = {
        UID_A: FakeSubject("acc-a", 30),
        UID_B: FakeSubject("acc-b", 5),
    }
    profiles = [
        FakeProfile("acc-a", "王小明"),
        FakeProfile("acc-b", "王小明的媽媽"),
        FakeProfile("acc-orphan", "沒綁定的人"),
    ]
    return MemberDirectory(backend=FakeBackend(subjects)), FakeSession(profiles)


# ---------------------------------------------------------------- uid format


@pytest.mark.parametrize("value", [UID_A, "U" + "0123456789abcdef" * 2])
def test_recognises_line_uid(value):
    assert looks_like_line_uid(value)


@pytest.mark.parametrize("value", ["王小明", "U123", "U" + "z" * 32, "", "a" * 33])
def test_rejects_non_uid(value):
    assert not looks_like_line_uid(value)


# ---------------------------------------------------------------- lookup


def test_find_by_uid_returns_subject_and_profile(directory_env):
    directory, session = directory_env
    match = directory.find_by_uid(session, UID_A)
    assert match.line_uid == UID_A
    assert match.subject.points_balance == 30
    assert display_name_of(match) == "王小明"


def test_find_by_uid_missing_member(directory_env):
    directory, session = directory_env
    assert directory.find_by_uid(session, "U" + "c" * 32) is None


def test_find_by_name_skips_profiles_without_line_identity(directory_env):
    """A profile with no LINE identity can't be credited — it must not be offered."""
    directory, session = directory_env
    names = [display_name_of(m) for m in directory.find_by_name(session, "王")]
    assert names == ["王小明", "王小明的媽媽"]


def test_search_uses_exact_lookup_for_uid(directory_env):
    directory, session = directory_env
    matches = directory.search(session, UID_B)
    assert [m.line_uid for m in matches] == [UID_B]


def test_search_falls_back_to_name(directory_env):
    directory, session = directory_env
    assert len(directory.search(session, "王")) == 2


def test_display_name_falls_back_when_no_profile():
    assert display_name_of(MemberMatch(FakeSubject("x", 0), None, UID_A)) == "使用者"


# ---------------------------------------------------------------- selection


def _matches(count):
    return [MemberMatch(FakeSubject(f"acc-{i}", i), FakeProfile(f"acc-{i}", f"名字{i}"), "U" + str(i) * 32)
            for i in range(count)]


def test_select_match_none_found():
    assert select_match([], "查無此人", assume_yes=False, out=lambda *a: None) is None


def test_select_match_single_needs_no_prompt():
    only = _matches(1)[0]
    picked = select_match([only], "名字0", assume_yes=False,
                          prompt=_forbidden_prompt, out=lambda *a: None)
    assert picked is only


def _forbidden_prompt(_):
    raise AssertionError("只有一位相符時不該再問")


def test_select_match_picks_by_number():
    matches = _matches(3)
    picked = select_match(matches, "名字", assume_yes=False,
                          prompt=lambda _: "2", out=lambda *a: None)
    assert picked is matches[1]


@pytest.mark.parametrize("answer", ["", "0", "4", "abc"])
def test_select_match_rejects_bad_answer(answer):
    """空白或超出範圍都不選人——加錯人比多問一次糟得多"""
    picked = select_match(_matches(3), "名字", assume_yes=False,
                          prompt=lambda _: answer, out=lambda *a: None)
    assert picked is None


def test_select_match_refuses_to_guess_in_yes_mode():
    picked = select_match(_matches(2), "名字", assume_yes=True,
                          prompt=_forbidden_prompt, out=lambda *a: None)
    assert picked is None


# ---------------------------------------------------------------- confirmation


@pytest.mark.parametrize("answer,expected", [
    ("y", True), ("Y", True), ("yes", True), ("是", True),
    ("n", False), ("", False), ("nope", False),
])
def test_confirm(answer, expected):
    target = {'display_name': "王小明", 'line_uid': UID_A, 'points': 30}
    assert confirm(target, 50, "管理員手動增加",
                   prompt=lambda _: answer, out=lambda *a: None) is expected


# ---------------------------------------------------------------- formatting


@pytest.mark.parametrize("text,width,expected", [
    ("abc", 5, "abc  "),
    ("王小明", 8, "王小明  "),          # 中文一字兩欄
    ("測試阿嬤的鄰居", 8, "測試阿嬤"),   # 超過就裁掉
    ("", 3, "   "),
])
def test_fit_pads_and_truncates_by_display_width(text, width, expected):
    """欄位要用終端顯示寬度對齊，否則中文名字會把 UID 欄推歪、看錯行選錯人"""
    assert _fit(text, width) == expected
