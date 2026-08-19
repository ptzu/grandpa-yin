"""Locally-served thumbnails — the no-cloud fallback for video messages.

A video message must carry a thumbnail URL that LINE's servers fetch themselves,
so the bytes are not enough: something has to answer an HTTP GET. In production
that's Supabase Storage (a signed URL). This is the fallback for when Storage
isn't configured — local development behind ngrok — where the app serves the
thumbnail itself from `/preview/<token>`.

Two things make that possible:

* the public base URL is taken from the incoming webhook request (ngrok locally,
  Railway in production), so nothing has to be configured;
* the file outlives the conversation state, because LINE fetches the thumbnail
  whenever it renders the message — possibly minutes later.

Files are unguessable and expire; this is a dev convenience, not a CDN.
"""
import contextvars
import os
import re
import tempfile
import time
import uuid

from src.core.app_logger import get_logger

logger = get_logger("preview_store")

# Set per-request in app.py, read when a feature needs to build a public URL.
# Background tasks inherit it because task_executor copies the context.
_base_url_var = contextvars.ContextVar("public_base_url", default=None)

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_DEFAULT_TTL_HOURS = 24


def set_public_base_url(url: str):
    """Remember this request's own public origin (e.g. https://xxx.ngrok-free.app)."""
    _base_url_var.set((url or "").rstrip("/") or None)


def get_public_base_url():
    return _base_url_var.get()


class LocalPreviewStore:
    """Writes thumbnails to a temp dir and serves them over the app's own URL."""

    def __init__(self, directory: str = None, ttl_hours: int = _DEFAULT_TTL_HOURS):
        self.directory = directory or os.path.join(tempfile.gettempdir(), "grandpa_yin_previews")
        self.ttl_seconds = ttl_hours * 3600

    def is_available(self) -> bool:
        """Usable only when we know a public URL LINE could actually reach."""
        return bool(get_public_base_url())

    def save(self, image_bytes: bytes) -> str:
        """Store the image and return a public URL, or None if we have no origin."""
        base_url = get_public_base_url()
        if not base_url:
            logger.warning("尚未取得本服務的公開網址，無法產生本地縮圖連結")
            return None

        os.makedirs(self.directory, exist_ok=True)
        self._purge_expired()

        token = uuid.uuid4().hex
        with open(os.path.join(self.directory, token), "wb") as f:
            f.write(image_bytes)
        logger.debug(f"本地縮圖已存: {token} ({len(image_bytes)} bytes)")
        return f"{base_url}/preview/{token}"

    def load(self, token: str):
        """Bytes for a token, or None if unknown/expired. Rejects non-token input."""
        if not token or not _TOKEN_RE.match(token):
            # Fixed-shape tokens only — no path traversal, no enumeration by name
            return None
        path = os.path.join(self.directory, token)
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    def _purge_expired(self):
        """Best-effort sweep of old thumbnails; never breaks the caller."""
        cutoff = time.time() - self.ttl_seconds
        try:
            for name in os.listdir(self.directory):
                path = os.path.join(self.directory, name)
                try:
                    if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    continue
        except OSError:
            pass
