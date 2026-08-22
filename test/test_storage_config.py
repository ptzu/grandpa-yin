"""StorageService.is_configured 的判斷規則。

重點是：半填的設定（例如部署準備時直接複製 .env.example 的佔位值）不該被
當成「已設定」，否則上傳會硬去連 your-project.supabase.co 然後整個流程炸掉。
"""
import pytest

from src.services.storage_service import StorageService

SUPABASE_ENV = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_STORAGE_BUCKET")


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
