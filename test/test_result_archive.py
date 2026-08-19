"""成品保存：推給用戶的網址要能撐 30 天，而且保存失敗絕不能害用戶收不到。

這個功能的價值全在「用戶下個月回頭看還在」，所以測試盯兩件事：成品確實被
轉存成長效網址，以及每一條失敗路徑都還是把成品送到用戶手上。
"""
import pytest

from conftest import FAKE_OUTPUT_URL, RESULT_BYTES, build_env
from src.services.result_archive import RESULT_PREFIX, RETENTION_DAYS, ResultArchive

THIRTY_DAYS_SECONDS = RETENTION_DAYS * 86400


@pytest.fixture
def env():
    return build_env()


def run_colorize(env):
    env.send_text("修復老照片")
    env.send_image()


class TestArchiveOnDelivery:
    def test_result_is_stored_and_pushed_with_our_own_url(self, env):
        run_colorize(env)

        assert env.archive.downloaded == [FAKE_OUTPUT_URL], "應該去抓模型的成品"
        assert len(env.archived_objects) == 1, "成品應存成一個物件"

        pushed = env.pushed_media()
        assert pushed and pushed["type"] == "ImageSendMessage", "應推送結果圖片"
        url = pushed["media_url"]
        assert url != FAKE_OUTPUT_URL, "不能再推模型的暫存網址，一小時後會破圖"
        assert url.startswith("https://storage.test/signed/")

    def test_signed_url_lasts_the_full_retention(self, env):
        run_colorize(env)

        key, expires_in = env.storage.signed[-1]
        assert key.startswith(f"{RESULT_PREFIX}/")
        assert expires_in == THIRTY_DAYS_SECONDS, "網址有效期要跟保留承諾一致"

    def test_stored_under_the_results_prefix(self, env):
        """保留期是靠 prefix 分辨的；存錯地方會被 24 小時的清理規則掃掉"""
        run_colorize(env)

        assert all(k.startswith(f"{RESULT_PREFIX}/") for k in env.archived_objects)
        assert env.stashed_objects == {}, "暫存原圖仍應照常刪除"


class TestArchiveFailureNeverCostsTheUser:
    def test_falls_back_to_the_model_url_when_download_fails(self, env):
        env.archive.download_fails = True

        run_colorize(env)

        pushed = env.pushed_media()
        assert pushed, "保存失敗也一定要把成品送到用戶手上"
        assert pushed["media_url"] == FAKE_OUTPUT_URL
        assert env.member.refunds == [], "成品送達就不該退點"

    def test_falls_back_when_storage_is_unavailable(self):
        env = build_env()
        env.archive._storage = None

        run_colorize(env)

        assert env.pushed_media()["media_url"] == FAKE_OUTPUT_URL

    def test_no_archive_wired_at_all_still_delivers(self):
        """Storage 沒設定的降級環境：行為回到保存功能上線前的樣子"""
        env = build_env()
        for feature in env.registry.features:
            feature.result_archive = None

        run_colorize(env)

        assert env.pushed_media()["media_url"] == FAKE_OUTPUT_URL


class TestTypeResolution:
    """型別認錯會存出一個 LINE 打不開的檔案，所以寧可不存"""

    @pytest.mark.parametrize("content_type,url,expected", [
        ("image/jpeg", "https://x.test/out", ("jpg", "image/jpeg")),
        ("video/mp4", "https://x.test/out", ("mp4", "video/mp4")),
        # Content-Type 帶參數的情況由 _download 切乾淨後才進來
        ("image/png", "https://x.test/out.jpg", ("png", "image/png")),
        # Content-Type 問不出來時才看網址
        ("", "https://x.test/out.mp4", ("mp4", "video/mp4")),
        ("application/octet-stream", "https://x.test/out.jpeg", ("jpg", "image/jpeg")),
    ])
    def test_resolves_known_types(self, content_type, url, expected):
        assert ResultArchive._resolve_type(content_type, url) == expected

    @pytest.mark.parametrize("content_type,url", [
        ("", "https://x.test/out"),
        ("text/html", "https://x.test/oops"),
        ("application/octet-stream", "https://x.test/out.exe"),
    ])
    def test_unknown_types_are_refused(self, content_type, url):
        with pytest.raises(ValueError):
            ResultArchive._resolve_type(content_type, url)


class TestStoreBytes:
    def test_stores_with_the_retention_ttl(self, env):
        url = env.archive.store_bytes(RESULT_BYTES)

        assert url.startswith("https://storage.test/signed/")
        key, expires_in = env.storage.signed[-1]
        assert key.startswith(f"{RESULT_PREFIX}/")
        assert expires_in == THIRTY_DAYS_SECONDS

    def test_returns_none_when_unavailable(self, env):
        env.archive._storage = None

        assert env.archive.store_bytes(RESULT_BYTES) is None
