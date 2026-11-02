"""Runtime settings. Secrets are read by name and never logged."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

KEY_FILE = Path.home() / ".config" / "delegate" / "model_api_key"


def load_api_key() -> str | None:
    """Return the Meta Model API key from MODEL_API_KEY or the owner's key file, else None."""
    value = os.environ.get("MODEL_API_KEY", "").strip()
    if value:
        return value
    try:
        text = KEY_FILE.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return None
    return text or None


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    meta_api_base: str = field(default_factory=lambda: os.environ.get("META_API_BASE", "https://api.meta.ai/v1"))
    model: str = field(default_factory=lambda: os.environ.get("DELEGATE_MODEL", "muse-spark-1.3"))
    audio_model: str = field(default_factory=lambda: os.environ.get("DELEGATE_AUDIO_MODEL", "muse-spark-1.2"))
    asr_model: str = "muse-voice-transcribe-1.0"
    provider_mode: str = field(default_factory=lambda: os.environ.get("DELEGATE_PROVIDER_MODE", "live"))
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))
    pgdata_dir: str = field(default_factory=lambda: os.environ.get("DELEGATE_PGDATA", ".pgdata"))
    web_dist: str = field(default_factory=lambda: os.environ.get("DELEGATE_WEB_DIST", "apps/web/dist"))
    sessions_per_ip_per_hour: int = field(default_factory=lambda: _int("DELEGATE_SESSIONS_PER_IP_PER_HOUR", 6))
    provider_concurrency: int = field(default_factory=lambda: _int("DELEGATE_PROVIDER_CONCURRENCY", 4))
    provider_timeout_s: float = 45.0
    session_ttl_s: int = 24 * 3600
    mandate_lifetime_s: int = 30 * 60
    max_provider_calls_per_run: int = 24
    max_provider_calls_per_session: int = 60
    max_live_calls_per_day: int = field(default_factory=lambda: _int("DELEGATE_MAX_LIVE_CALLS_PER_DAY", 1500))
    max_provider_calls_per_step: int = 4
    max_previews_per_step: int = 2
    max_invalid_corrections: int = 2
    max_buyer_turns_per_run: int = 20
    max_audio_bytes: int = 4 * 1024 * 1024
    run_worker: bool = field(default_factory=lambda: os.environ.get("DELEGATE_RUN_WORKER", "1") == "1")

    def resolved_provider_mode(self) -> str:
        """'live' only when a key exists; otherwise 'unavailable' unless mock was explicitly chosen."""
        if self.provider_mode == "mock":
            return "mock"
        return "live" if load_api_key() else "unavailable"
