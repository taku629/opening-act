"""Runtime configuration. Everything is read from environment variables so that
switching from demo data to the live Qloo API is a config-only change."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass
class Settings:
    qloo_api_key: str = field(default_factory=lambda: os.getenv("QLOO_API_KEY", "").strip())
    qloo_base_url: str = field(
        default_factory=lambda: os.getenv("QLOO_BASE_URL", "https://hackathon.api.qloo.com").rstrip("/")
    )
    # QLOO_MOCK=1 forces demo data, QLOO_MOCK=0 forces live. Unset = auto (mock when no key).
    qloo_mock_env: str = field(default_factory=lambda: os.getenv("QLOO_MOCK", "").strip().lower())
    qloo_timeout_s: float = field(default_factory=lambda: float(os.getenv("QLOO_TIMEOUT_S", "15") or 15))
    qloo_max_retries: int = field(default_factory=lambda: _int("QLOO_MAX_RETRIES", 2))
    cache_path: str = field(default_factory=lambda: os.getenv("CACHE_PATH", "/tmp/opening_act_cache.sqlite"))
    cache_ttl_s: int = field(default_factory=lambda: _int("CACHE_TTL_S", 60 * 60 * 24))
    # Heatmap output boundary. Empty = API default (geohash grid), which is the documented shape.
    heatmap_boundary: str = field(default_factory=lambda: os.getenv("QLOO_HEATMAP_BOUNDARY", "").strip())

    llm_base_url: str = field(
        default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.tokenfactory.nebius.com/v1/").strip()
    )
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", "").strip())
    llm_model: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL", "Qwen/Qwen3-235B-A22B-Instruct-2507").strip()
    )
    llm_timeout_s: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT_S", "45") or 45))
    llm_disabled: bool = field(default_factory=lambda: _bool("LLM_DISABLED", False))

    rate_limit_per_hour: int = field(default_factory=lambda: _int("RATE_LIMIT_PER_HOUR", 30))
    trust_proxy: bool = field(default_factory=lambda: _bool("TRUST_PROXY", True))

    @property
    def qloo_mock(self) -> bool:
        if self.qloo_mock_env in {"1", "true", "yes", "on"}:
            return True
        if self.qloo_mock_env in {"0", "false", "no", "off"}:
            return False
        return not self.qloo_api_key

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key) and not self.llm_disabled


def get_settings() -> Settings:
    return Settings()
