"""Loads config/settings.yml — the operator-editable settings: which AI model
each image feature uses, what it costs, and what new members are given.

Model IDs, prices and the signup bonus used to be constants on the feature
classes and one-off `os.getenv` calls, so changing any of them meant a code
change, and two places (the feature and the help text) could disagree about the
price. They now come from one file.

The input mapping lives here too because a model ID alone is not swappable:
Replicate models disagree about what the image field is called and whether it
takes a list, so a config that only carried the ID would break the moment
someone used it.

Invalid config raises rather than falling back to something silently different —
a wrong model or a wrong price is worse than a failed deploy. `main()` is wired
into CI and Railway's preDeployCommand so it never gets that far.
"""
import os
import re
from dataclasses import dataclass, field
from typing import Optional

import yaml

from src.core.app_logger import get_logger

logger = get_logger("settings")

# config/settings.yml, resolved from this file so cwd doesn't matter
_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "settings.yml",
)

# LINE only accepts 5..60 in multiples of 5 for the loading animation
_LOADING_MIN = 5
_LOADING_MAX = 60
_LOADING_STEP = 5

# 模型呼叫的整體等待上限。這道防線要砍的是「永遠不會回來」的工作，不是「今天
# 比較慢」的工作——誤砍等於白白退點又讓用戶重來，所以預設抓得寬鬆。
DEFAULT_TIMEOUT_SECONDS = 300
_TIMEOUT_MIN = 30
_TIMEOUT_MAX = 1800


class SettingsError(RuntimeError):
    """設定檔有誤。訊息會直接呈現給操作者，寫清楚哪個欄位、該怎麼改。"""


@dataclass(frozen=True)
class ModelConfig:
    """One image feature's model, price and input shape."""

    feature: str
    model: str
    cost: int
    loading_seconds: int
    image_field: str
    image_is_list: bool
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    prompt_field: Optional[str] = None
    default_prompt: Optional[str] = None
    extra_input: dict = field(default_factory=dict)

    def build_input(self, image_data_url: str, prompt: str = None) -> dict:
        """Assemble the payload for this model from the configured field names.

        Features where the user writes the instruction (edit) pass `prompt`.
        Features where the instruction is fixed (animate) leave it out and get
        `default_prompt` from the config — that wording is the main quality dial
        for such a feature, so it belongs somewhere tunable without a deploy.
        """
        payload = dict(self.extra_input)
        payload[self.image_field] = [image_data_url] if self.image_is_list else image_data_url

        text = prompt if prompt is not None else self.default_prompt
        if text is not None:
            if not self.prompt_field:
                raise SettingsError(
                    f"設定檔的 {self.feature}.input.prompt_field 是空的，"
                    f"但 {self.feature} 需要送出文字描述。請填入該模型接收描述的欄位名稱。"
                )
            payload[self.prompt_field] = text
        return payload


def _require(section: dict, feature: str, key: str):
    if key not in section or section[key] is None:
        raise SettingsError(f"設定檔缺少 {feature}.{key}")
    return section[key]


def _parse_feature(feature: str, section) -> ModelConfig:
    if not isinstance(section, dict):
        raise SettingsError(f"設定檔的 {feature} 必須是一組設定（縮排的 key: value），實際是 {type(section).__name__}")

    model = _require(section, feature, "model")
    if not isinstance(model, str) or "/" not in model:
        raise SettingsError(
            f"{feature}.model 應為 Replicate 模型 ID（格式：作者/模型名），實際是 {model!r}"
        )

    cost = _require(section, feature, "cost")
    if not isinstance(cost, int) or isinstance(cost, bool) or cost < 0:
        raise SettingsError(f"{feature}.cost 必須是 0 或正整數，實際是 {cost!r}")

    loading_seconds = section.get("loading_seconds", 30)
    if (not isinstance(loading_seconds, int) or isinstance(loading_seconds, bool)
            or not _LOADING_MIN <= loading_seconds <= _LOADING_MAX
            or loading_seconds % _LOADING_STEP != 0):
        raise SettingsError(
            f"{feature}.loading_seconds 必須是 {_LOADING_MIN}～{_LOADING_MAX} 之間、"
            f"{_LOADING_STEP} 的倍數（LINE 的限制），實際是 {loading_seconds!r}"
        )

    timeout_seconds = section.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if (not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool)
            or not _TIMEOUT_MIN <= timeout_seconds <= _TIMEOUT_MAX):
        raise SettingsError(
            f"{feature}.timeout_seconds 必須是 {_TIMEOUT_MIN}～{_TIMEOUT_MAX} 之間的整數"
            f"（模型跑多久算吊死），實際是 {timeout_seconds!r}"
        )

    input_section = section.get("input")
    if not isinstance(input_section, dict):
        raise SettingsError(
            f"設定檔缺少 features.{feature}.input——換模型時必須指定該模型的欄位名稱，"
            f"否則呼叫會失敗。範例見 config/settings.yml 的註解。"
        )

    image_field = _require(input_section, f"{feature}.input", "image_field")
    if not isinstance(image_field, str):
        raise SettingsError(f"{feature}.input.image_field 必須是欄位名稱字串，實際是 {image_field!r}")

    image_is_list = input_section.get("image_is_list", False)
    if not isinstance(image_is_list, bool):
        raise SettingsError(
            f"{feature}.input.image_is_list 必須是 true 或 false，實際是 {image_is_list!r}"
        )

    prompt_field = input_section.get("prompt_field")
    if prompt_field is not None and not isinstance(prompt_field, str):
        raise SettingsError(
            f"{feature}.input.prompt_field 必須是欄位名稱字串，或留 null 表示此模型不吃描述"
        )

    default_prompt = input_section.get("default_prompt")
    if default_prompt is not None:
        if not isinstance(default_prompt, str) or not default_prompt.strip():
            raise SettingsError(
                f"{feature}.input.default_prompt 必須是非空字串（此功能固定送出的指令），或整個省略"
            )
        if not prompt_field:
            raise SettingsError(
                f"{feature}.input 設了 default_prompt 卻沒有 prompt_field——"
                f"沒有欄位可以放這段指令。請補上該模型接收描述的欄位名稱。"
            )
        default_prompt = default_prompt.strip()

    extra_input = section.get("extra_input") or {}
    if not isinstance(extra_input, dict):
        raise SettingsError(f"{feature}.extra_input 必須是一組 key: value，實際是 {type(extra_input).__name__}")

    return ModelConfig(
        feature=feature,
        model=model,
        cost=cost,
        loading_seconds=loading_seconds,
        timeout_seconds=timeout_seconds,
        image_field=image_field,
        image_is_list=image_is_list,
        prompt_field=prompt_field,
        default_prompt=default_prompt,
        extra_input=extra_input,
    )


def _apply_env_overrides(config: ModelConfig) -> ModelConfig:
    """Let Railway Variables override model, cost and timeout without a redeploy.

    Only these scalars are overridable — the field mapping belongs with the
    model it describes, and splitting them across two places invites a mismatch.
    Timeout is in the list because the moment it matters is an incident: the
    model got slower, jobs are being cancelled, and redeploying to widen the
    limit is the slowest possible fix.
    """
    prefix = config.feature.upper()
    model = os.getenv(f"{prefix}_MODEL")
    cost = os.getenv(f"{prefix}_COST")
    timeout = os.getenv(f"{prefix}_TIMEOUT")

    if not model and not cost and not timeout:
        return config

    changes = {}
    if model:
        changes["model"] = model
        logger.info(f"{config.feature}.model 被環境變數 {prefix}_MODEL 覆寫為 {model}")
    if cost:
        try:
            changes["cost"] = int(cost)
        except ValueError:
            raise SettingsError(f"環境變數 {prefix}_COST 必須是整數，實際是 {cost!r}")
        logger.info(f"{config.feature}.cost 被環境變數 {prefix}_COST 覆寫為 {cost}")
    if timeout:
        try:
            changes["timeout_seconds"] = int(timeout)
        except ValueError:
            raise SettingsError(f"環境變數 {prefix}_TIMEOUT 必須是整數，實際是 {timeout!r}")
        logger.info(
            f"{config.feature}.timeout_seconds 被環境變數 {prefix}_TIMEOUT 覆寫為 {timeout}"
        )

    return ModelConfig(**{**config.__dict__, **changes})


@dataclass(frozen=True)
class MemberSettings:
    """Settings that apply to members rather than to any one feature."""

    welcome_points: int


def _parse_members(section) -> MemberSettings:
    section = section or {}
    if not isinstance(section, dict):
        raise SettingsError(
            f"設定檔的 members 必須是一組設定（縮排的 key: value），實際是 {type(section).__name__}"
        )

    welcome_points = section.get("welcome_points", 0)
    env_override = os.getenv("WELCOME_POINTS")
    if env_override:
        try:
            welcome_points = int(env_override)
        except ValueError:
            raise SettingsError(f"環境變數 WELCOME_POINTS 必須是整數，實際是 {env_override!r}")
        logger.info(f"members.welcome_points 被環境變數 WELCOME_POINTS 覆寫為 {welcome_points}")

    if not isinstance(welcome_points, int) or isinstance(welcome_points, bool) or welcome_points < 0:
        raise SettingsError(
            f"members.welcome_points 必須是 0 或正整數（0 表示不送），實際是 {welcome_points!r}"
        )

    return MemberSettings(welcome_points=welcome_points)


@dataclass(frozen=True)
class PointPackage:
    """One thing a user can buy: a fixed number of points for a fixed price."""

    id: str
    points: int
    price_twd: int
    label: str


@dataclass(frozen=True)
class PaymentSettings:
    """Top-up settings. Absent from the file means top-up is simply off, which
    is the state every existing deployment is in — so it must stay valid."""

    provider: str
    packages: tuple

    @property
    def enabled(self) -> bool:
        return bool(self.provider and self.packages)

    def package(self, package_id):
        """The package with this id, or None. Callers must treat None as
        "unknown package" — never fall back to a default, or a tampered id
        would pick the cheapest price with the largest point grant."""
        for pkg in self.packages:
            if pkg.id == package_id:
                return pkg
        return None


SUPPORTED_PROVIDERS = ("ecpay",)

# Package ids end up inside the payment provider's order number, which ECPay
# limits to 20 alphanumeric characters — so keep them short and boring.
_PACKAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


def _parse_package(index, raw) -> PointPackage:
    if not isinstance(raw, dict):
        raise SettingsError(
            f"payments.packages 第 {index} 項必須是一組設定（縮排的 key: value），"
            f"實際是 {type(raw).__name__}"
        )

    package_id = raw.get("id")
    if not isinstance(package_id, str) or not _PACKAGE_ID_PATTERN.match(package_id):
        raise SettingsError(
            f"payments.packages 第 {index} 項的 id 必須是 1～16 個英數字、底線或減號，"
            f"實際是 {package_id!r}"
        )

    def positive_int(key):
        value = raw.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise SettingsError(
                f"payments.packages 的 {package_id} 的 {key} 必須是正整數，實際是 {value!r}"
            )
        return value

    points = positive_int("points")
    price_twd = positive_int("price_twd")

    label = raw.get("label") or f"{points} 點"
    if not isinstance(label, str):
        raise SettingsError(
            f"payments.packages 的 {package_id} 的 label 必須是文字，實際是 {label!r}"
        )

    return PointPackage(id=package_id, points=points, price_twd=price_twd, label=label)


def _parse_payments(section) -> PaymentSettings:
    """No payments section = top-up disabled. A half-filled one is an error:
    silently disabling a payment feature because of a typo is worse than
    refusing to boot."""
    if section is None:
        return PaymentSettings(provider="", packages=())

    if not isinstance(section, dict):
        raise SettingsError(
            f"設定檔的 payments 必須是一組設定（縮排的 key: value），"
            f"實際是 {type(section).__name__}"
        )

    provider = section.get("provider")
    if provider not in SUPPORTED_PROVIDERS:
        raise SettingsError(
            f"payments.provider 目前只支援 {list(SUPPORTED_PROVIDERS)}，實際是 {provider!r}"
        )

    packages = section.get("packages")
    if not isinstance(packages, list) or not packages:
        raise SettingsError(
            "payments.packages 必須是至少一項的清單（每項要有 id / points / price_twd）"
        )

    parsed = tuple(_parse_package(i, raw) for i, raw in enumerate(packages, start=1))

    seen = [p.id for p in parsed]
    duplicates = {i for i in seen if seen.count(i) > 1}
    if duplicates:
        raise SettingsError(
            f"payments.packages 的 id 不可重複，重複的是：{sorted(duplicates)}"
        )

    return PaymentSettings(provider=provider, packages=parsed)


@dataclass(frozen=True)
class Settings:
    """The whole settings file, validated."""

    features: dict
    members: MemberSettings
    payments: PaymentSettings


def load_settings(path: str = None) -> Settings:
    """Read and validate the settings file. Raises SettingsError if bad."""
    path = path or os.getenv("SETTINGS_PATH") or _DEFAULT_PATH

    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raise SettingsError(
            f"找不到設定檔：{path}\n"
            f"請確認 config/settings.yml 存在，或用 SETTINGS_PATH 指定位置。"
        )
    except yaml.YAMLError as e:
        raise SettingsError(f"設定檔 {path} 格式有誤（YAML 解析失敗）：{e}")

    if not isinstance(raw, dict) or not raw:
        raise SettingsError(f"設定檔 {path} 是空的或格式不對")

    features_section = raw.get("features")
    if not isinstance(features_section, dict) or not features_section:
        raise SettingsError(
            f"設定檔 {path} 缺少 features 區段（各功能的模型與點數）。範例見檔案內註解。"
        )

    features = {
        feature: _apply_env_overrides(_parse_feature(feature, section))
        for feature, section in features_section.items()
    }
    members = _parse_members(raw.get("members"))
    payments = _parse_payments(raw.get("payments"))

    logger.info(
        "設定已載入："
        + "、".join(f"{name}={c.model}（{c.cost} 點）" for name, c in features.items())
        + f"、新會員贈點 {members.welcome_points}"
        + (f"、儲值 {payments.provider}（{len(payments.packages)} 種點數包）"
           if payments.enabled else "、儲值未啟用")
    )
    return Settings(features=features, members=members, payments=payments)


_cache = None


def _settings() -> Settings:
    global _cache
    if _cache is None:
        _cache = load_settings()
    return _cache


def get_model_config(feature: str) -> ModelConfig:
    """Model/price config for one feature, loading the file on first use."""
    features = _settings().features
    if feature not in features:
        raise SettingsError(
            f"設定檔的 features 沒有 {feature} 這一段。已設定的功能：{sorted(features)}"
        )
    return features[feature]


def get_member_settings() -> MemberSettings:
    """Member-wide settings (signup bonus)."""
    return _settings().members


def get_payment_settings() -> PaymentSettings:
    """Top-up settings; `.enabled` is False when the file has no payments section."""
    return _settings().payments


def reset_cache():
    """Drop the cached file so the next read re-loads it (tests use this)."""
    global _cache
    _cache = None


def main():
    """Validate the config and report it.

    Wired into Railway's preDeployCommand so a bad edit aborts the deploy: the
    app itself swallows startup errors and retries per request, which would put
    a broken config live and turn every webhook into a 500 instead.

    Also the local pre-push check:  python3 -m src.core.settings
    """
    import sys

    try:
        from dotenv import load_dotenv
        load_dotenv()  # so local runs see the same *_COST / *_MODEL overrides
    except ImportError:
        pass

    try:
        settings = load_settings()
    except SettingsError as e:
        print(f"\n❌ 設定檢查失敗：\n\n{e}\n", file=sys.stderr)
        return 1

    print("\n✅ 設定正常，實際生效的值：\n")
    for name, c in sorted(settings.features.items()):
        prompt = c.prompt_field or "（不送描述）"
        print(f"  {name}")
        print(f"    模型：{c.model}")
        print(f"    點數：{c.cost} 點　載入動畫：{c.loading_seconds} 秒"
              f"　逾時：{c.timeout_seconds} 秒")
        print(f"    輸入欄位：{c.image_field}"
              f"{'（陣列）' if c.image_is_list else '（單值）'}　描述欄位：{prompt}")
        if c.extra_input:
            print(f"    額外參數：{c.extra_input}")
        print()

    bonus = settings.members.welcome_points
    print("  members")
    print(f"    新會員贈點：{bonus} 點" + ("　（0＝不送）" if bonus == 0 else ""))
    print()

    payments = settings.payments
    print("  payments")
    if not payments.enabled:
        print("    儲值未啟用（設定檔沒有 payments 區段）")
    else:
        print(f"    金流：{payments.provider}")
        for pkg in payments.packages:
            print(f"    {pkg.id:<8} {pkg.points:>6} 點　NT${pkg.price_twd:<6} {pkg.label}")
    print()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
