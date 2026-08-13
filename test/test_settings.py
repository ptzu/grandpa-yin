"""config/settings.yml — loading, validation, and the fact that it actually
drives which model gets called with what payload.

A config file that silently doesn't take effect would be worse than no config
file at all, so the last class here checks the whole way through to the
Replicate call.
"""
import pytest
import yaml

from src.core.settings import (
    SettingsError, get_member_settings, get_model_config, load_settings, reset_cache,
)

from conftest import build_env

FEATURES = {
    "edit": {
        "model": "google/nano-banana",
        "cost": 5,
        "loading_seconds": 45,
        "input": {"image_field": "image_input", "image_is_list": True, "prompt_field": "prompt"},
        "extra_input": {"output_format": "jpg"},
    },
    "colorize": {
        "model": "flux-kontext-apps/restore-image",
        "cost": 10,
        "loading_seconds": 30,
        "input": {"image_field": "input_image", "image_is_list": False, "prompt_field": None},
    },
}

VALID = {"features": FEATURES, "members": {"welcome_points": 50}}

IMAGE_URL = "data:image/jpeg;base64,ZmFrZQ=="


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    """Point the loader at a temp config file and keep the cache from leaking."""
    def write(raw, *, text=None):
        path = tmp_path / "models.yml"
        path.write_text(text if text is not None else yaml.safe_dump(raw), encoding="utf-8")
        monkeypatch.setenv("SETTINGS_PATH", str(path))
        reset_cache()
        return str(path)

    yield write
    reset_cache()


def deep_copy_valid(**edits):
    """A fresh copy of VALID with edits applied, keyed by dotted path.

    "edit.cost" edits features.edit.cost; "members" replaces that whole section.
    """
    raw = yaml.safe_load(yaml.safe_dump(VALID))
    for path, value in edits.items():
        head, _, key = path.partition(".")
        if head == "members":
            raw["members"] = value if not key else {**raw["members"], key: value}
        elif key:
            raw["features"][head][key] = value
        else:
            raw["features"][head] = value
    return raw


class TestLoading:
    def test_reads_model_and_cost(self, write_config):
        write_config(VALID)
        edit = get_model_config("edit")

        assert edit.model == "google/nano-banana"
        assert edit.cost == 5
        assert edit.loading_seconds == 45

    def test_loading_seconds_defaults_to_30(self, write_config):
        raw = deep_copy_valid()
        del raw["features"]["edit"]["loading_seconds"]
        write_config(raw)

        assert get_model_config("edit").loading_seconds == 30

    def test_unknown_feature_names_the_available_ones(self, write_config):
        write_config(VALID)

        with pytest.raises(SettingsError, match="colorize"):
            get_model_config("upscale")

    def test_shipped_config_is_valid(self):
        """出貨的 config/settings.yml 本身必須通過驗證（CI 靠這條把關）"""
        reset_cache()
        settings = load_settings()
        assert {"edit", "colorize"} <= set(settings.features)
        assert settings.members.welcome_points >= 0


class TestBuildInput:
    def test_list_style_image_field(self, write_config):
        write_config(VALID)

        payload = get_model_config("edit").build_input(IMAGE_URL, prompt="加上彩虹")

        assert payload == {
            "image_input": [IMAGE_URL],
            "prompt": "加上彩虹",
            "output_format": "jpg",
        }

    def test_scalar_image_field_and_no_prompt(self, write_config):
        write_config(VALID)

        payload = get_model_config("colorize").build_input(IMAGE_URL)

        assert payload == {"input_image": IMAGE_URL}

    def test_prompt_without_a_prompt_field_is_a_config_error(self, write_config):
        write_config(VALID)

        with pytest.raises(SettingsError, match="prompt_field"):
            get_model_config("colorize").build_input(IMAGE_URL, prompt="加上彩虹")

    def test_extra_input_is_not_shared_between_calls(self, write_config):
        write_config(VALID)
        config = get_model_config("edit")

        config.build_input(IMAGE_URL, prompt="第一次")
        payload = config.build_input(IMAGE_URL, prompt="第二次")

        assert payload["prompt"] == "第二次"
        assert config.extra_input == {"output_format": "jpg"}, "設定本身不可被呼叫弄髒"


class TestMemberSettings:
    def test_reads_welcome_points(self, write_config):
        write_config(VALID)

        assert get_member_settings().welcome_points == 50

    def test_zero_means_no_bonus(self, write_config):
        write_config(deep_copy_valid(**{"members.welcome_points": 0}))

        assert get_member_settings().welcome_points == 0

    def test_missing_members_section_means_no_bonus(self, write_config):
        raw = deep_copy_valid()
        del raw["members"]
        write_config(raw)

        assert get_member_settings().welcome_points == 0

    def test_negative_bonus_is_rejected(self, write_config):
        write_config(deep_copy_valid(**{"members.welcome_points": -5}))

        with pytest.raises(SettingsError, match="welcome_points"):
            get_member_settings()

    def test_env_var_wins(self, write_config, monkeypatch):
        monkeypatch.setenv("WELCOME_POINTS", "80")
        write_config(VALID)

        assert get_member_settings().welcome_points == 80


class TestEnvOverrides:
    def test_cost_env_var_wins(self, write_config, monkeypatch):
        monkeypatch.setenv("EDIT_COST", "12")
        write_config(VALID)

        assert get_model_config("edit").cost == 12

    def test_model_env_var_wins(self, write_config, monkeypatch):
        monkeypatch.setenv("EDIT_MODEL", "someone/other-model")
        write_config(VALID)

        assert get_model_config("edit").model == "someone/other-model"

    def test_non_numeric_cost_env_var_is_rejected(self, write_config, monkeypatch):
        monkeypatch.setenv("EDIT_COST", "五點")

        with pytest.raises(SettingsError, match="EDIT_COST"):
            write_config(VALID)
            get_model_config("edit")


class TestValidation:
    """壞設定要在啟動時就報錯，而且訊息要說得出哪裡錯"""

    @pytest.mark.parametrize("edits,expected", [
        ({"edit.model": "nano-banana"}, "作者/模型名"),
        ({"edit.cost": "五"}, "cost"),
        ({"edit.cost": -1}, "cost"),
        ({"edit.loading_seconds": 7}, "倍數"),
        ({"edit.loading_seconds": 120}, "loading_seconds"),
        ({"edit.input": {"image_is_list": True}}, "image_field"),
        ({"edit.input": None}, "input"),
        ({"edit.input": {"image_field": "img", "image_is_list": "yes"}}, "image_is_list"),
        ({"edit.extra_input": ["output_format"]}, "extra_input"),
        ({"edit": "google/nano-banana"}, "必須是一組設定"),
    ], ids=[
        "model 沒有作者前綴", "cost 不是數字", "cost 是負數",
        "loading_seconds 非 5 的倍數", "loading_seconds 超過上限",
        "input 缺 image_field", "缺整個 input 區段",
        "image_is_list 不是布林", "extra_input 不是 key: value",
        "整段功能不是設定",
    ])
    def test_rejects_bad_config(self, write_config, edits, expected):
        write_config(deep_copy_valid(**edits))

        with pytest.raises(SettingsError, match=expected):
            get_model_config("edit")

    def test_missing_model_key(self, write_config):
        raw = deep_copy_valid()
        del raw["features"]["edit"]["model"]
        write_config(raw)

        with pytest.raises(SettingsError, match="edit.model"):
            get_model_config("edit")

    def test_missing_features_section(self, write_config):
        write_config({"members": {"welcome_points": 50}})

        with pytest.raises(SettingsError, match="features"):
            get_model_config("edit")

    def test_missing_file_says_where_it_looked(self, write_config, monkeypatch, tmp_path):
        monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "nope.yml"))
        reset_cache()

        with pytest.raises(SettingsError, match="找不到設定檔"):
            get_model_config("edit")

    def test_broken_yaml_is_reported_as_such(self, write_config):
        write_config(None, text="edit:\n  model: [unclosed\n")

        with pytest.raises(SettingsError, match="YAML"):
            get_model_config("edit")


class TestConfigReachesTheModelCall:
    """設定真的驅動了模型呼叫——不是載入了但沒人用"""

    def test_edit_sends_the_configured_model_and_payload(self, write_config):
        write_config(deep_copy_valid(**{"edit.model": "someone/custom-editor"}))
        env = build_env()

        env.send_image()
        env.send_text("照我說的修改")
        env.send_text("加上彩虹")
        env.send_text("確定開始")

        call = env.replicate.calls[0]
        assert call["model"] == "someone/custom-editor"
        assert call["input"]["prompt"] == "加上彩虹"
        assert isinstance(call["input"]["image_input"], list), "設定說 image_is_list: true"
        assert call["input"]["output_format"] == "jpg"

    def test_colorize_sends_a_scalar_image_field(self, write_config):
        write_config(VALID)
        env = build_env()

        env.send_image()
        env.send_text("幫照片上色")

        call = env.replicate.calls[0]
        assert call["model"] == "flux-kontext-apps/restore-image"
        assert isinstance(call["input"]["input_image"], str), "設定說 image_is_list: false"
        assert "prompt" not in call["input"]

    def test_changing_the_cost_changes_what_is_deducted(self, write_config):
        write_config(deep_copy_valid(**{"edit.cost": 21}))
        env = build_env()

        env.send_image()
        env.send_text("照我說的修改")
        env.send_text("加上彩虹")
        env.send_text("確定開始")

        assert env.member.deductions[0]["amount"] == 21

    def test_cost_shown_in_the_help_text_matches(self, write_config):
        write_config(deep_copy_valid(**{"edit.cost": 21, "colorize.cost": 33}))
        env = build_env()

        env.send_text("使用說明")

        assert "21 點" in env.last_text
        assert "33 點" in env.last_text
