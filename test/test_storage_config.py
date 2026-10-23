"""StorageService.is_configured 的判斷規則。

重點是：半填的設定（例如部署準備時直接複製 .env.example 的佔位值）不該被
當成「已設定」，否則上傳會硬去連 your-project.supabase.co 然後整個流程炸掉。
"""
import pytest

from src.services.storage_service import StorageService

SUPABASE_ENV = ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_STORAGE_BUCKET")


@pytest.fixture
def clean_env(monkeypatch):
    for key in SUPABASE_ENV:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


class TestIsConfigured:
    def test_real_values_are_configured(self, clean_env):
        clean_env.setenv("SUPABASE_URL", "https://abcd1234.supabase.co")
        clean_env.setenv("SUPABASE_SERVICE_ROLE_KEY", "eyJhbGciOi.real.key")
        assert StorageService().is_configured() is True

    def test_missing_values_are_not_configured(self, clean_env):
        assert StorageService().is_configured() is False

    def test_placeholder_url_is_not_configured(self, clean_env):
        """.env.example 的佔位 URL 要被當成未設定，才能退回 base64"""
        clean_env.setenv("SUPABASE_URL", "https://your-project.supabase.co")
        clean_env.setenv("SUPABASE_SERVICE_ROLE_KEY", "eyJhbGciOi.real.key")
        assert StorageService().is_configured() is False

    def test_placeholder_key_is_not_configured(self, clean_env):
        clean_env.setenv("SUPABASE_URL", "https://abcd1234.supabase.co")
        clean_env.setenv("SUPABASE_SERVICE_ROLE_KEY", "your_service_role_key")
        assert StorageService().is_configured() is False

    def test_placeholder_secret_key_is_not_configured(self, clean_env):
        clean_env.setenv("SUPABASE_URL", "https://abcd1234.supabase.co")
        clean_env.setenv("SUPABASE_SECRET_KEY", "your_secret_key")
        assert StorageService().is_configured() is False


class TestSecretKeyName:
    """新名 SUPABASE_SECRET_KEY 優先；舊名 SUPABASE_SERVICE_ROLE_KEY 當過渡 fallback"""

    def test_new_name_is_read(self, clean_env):
        clean_env.setenv("SUPABASE_URL", "https://abcd1234.supabase.co")
        clean_env.setenv("SUPABASE_SECRET_KEY", "sb_secret_new")
        service = StorageService()
        assert service.is_configured() is True
        assert service.api_key == "sb_secret_new"

    def test_legacy_name_still_works(self, clean_env):
        """Railway 變數還沒改名的環境不能因為這次改動而斷掉"""
        clean_env.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_legacy")
        assert StorageService().api_key == "sb_secret_legacy"

    def test_new_name_wins_when_both_set(self, clean_env):
        """輪替時兩個可能短暫並存，要確保用的是新那把"""
        clean_env.setenv("SUPABASE_SECRET_KEY", "sb_secret_new")
        clean_env.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_old_leaked")
        assert StorageService().api_key == "sb_secret_new"

    def test_empty_new_name_falls_back(self, clean_env):
        """新名設成空字串（Railway 留了空變數）時不該蓋掉仍有效的舊名"""
        clean_env.setenv("SUPABASE_SECRET_KEY", "")
        clean_env.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_legacy")
        assert StorageService().api_key == "sb_secret_legacy"
