"""狀態 log 不可以把整包 state_metadata 印出來。

實際踩過：Storage 未設定時 state 裡是整張 base64 照片，log 單行膨脹到
18 萬字元、檔案 2.7MB，排查事故時真正有用的那行被洗掉找不到。
"""
from src.services.user_state_manager import _loggable

HUGE = "/9j/4AAQSkZJRgABAQAA" + "A" * 200_000


class TestLoggableSummary:
    def test_long_values_are_summarised_not_printed(self):
        line = _loggable({"feature": "edit", "state": "waiting_confirm",
                          "data": {"image_data": HUGE}})

        assert HUGE[:100] not in line, "絕不可把 base64 內容印進 log"
        assert "image_data=<" in line and "字元>" in line, "應改為描述大小"
        assert len(line) < 200, f"摘要必須夠短，實際 {len(line)} 字元"

    def test_keeps_the_useful_parts(self):
        line = _loggable({"feature": "edit", "state": "waiting_confirm",
                          "data": {"image_key": "edit/a3f9.jpg", "description": "背景換成海灘"}})

        assert "feature=edit" in line
        assert "state=waiting_confirm" in line
        assert "edit/a3f9.jpg" in line, "短值照印，排查時用得到"
        assert "背景換成海灘" in line

    def test_handles_empty_and_missing(self):
        assert _loggable(None) == "無"
        assert _loggable({}) == "無"
        assert _loggable({"feature": "menu", "state": "idle", "data": None})

    def test_handles_non_dict_data(self):
        line = _loggable({"feature": "x", "state": "y", "data": ["a", "b"]})
        assert "list" in line
