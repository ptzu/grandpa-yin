"""正規指令集：一個功能一個詞，不做同義詞與模糊比對。

長輩主要靠 Quick Reply 按鈕操作，要打字時「一個功能一個詞」比較好記。
過去的寬鬆比對（例如 `"會員" in message`）還會把未來功能的指令一併吃掉。
"""
import pytest

from conftest import build_env
from src.features.feature_registry import GLOBAL_COMMANDS

TRIGGERS = ["修復老照片", "圖片編輯", "照片動起來"]
GLOBALS = ["點數", "歷史", "會員", "儲值", "功能", "使用說明"]


class TestCanonicalSet:
    def test_global_commands_are_exactly_these(self):
        assert sorted(GLOBAL_COMMANDS) == sorted(GLOBALS)

    @pytest.mark.parametrize("cmd", TRIGGERS)
    def test_every_trigger_routes_somewhere(self, cmd):
        env = build_env()
        env.send_text(cmd)
        assert env.messages, f"「{cmd}」應該要有回應"

    @pytest.mark.parametrize("cmd", GLOBALS)
    def test_every_global_command_responds(self, cmd):
        env = build_env(with_member_feature=True)
        env.send_text(cmd)
        assert env.messages, f"「{cmd}」應該要有回應"


class TestNoFuzzyMatching:
    """精簡掉的同義詞不該再被接受——否則等於沒精簡"""

    @pytest.mark.parametrize("cmd", [
        "!功能", "！功能", "其他功能",
        "點數查詢", "查看點數", "查詢點數",
        "交易記錄", "記錄", "會員資訊",
        "圖片彩色化",
    ])
    def test_retired_commands_are_gone(self, cmd):
        env = build_env(with_member_feature=True)
        env.send_text(cmd)
        assert env.messages == [], f"「{cmd}」已精簡掉，不該再有回應"

    def test_substring_does_not_trigger(self):
        """『我想查會員資料』不該被當成指令"""
        env = build_env(with_member_feature=True)
        env.send_text("我想查會員資料")
        assert env.messages == []
