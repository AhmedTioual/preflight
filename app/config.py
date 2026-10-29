"""Application settings, read from environment variables or a local .env file."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader; real environment variables always take precedence."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = "Preflight"
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./preflight.db"))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", "dev-only-change-me-in-production"))
    token_ttl_minutes: int = field(default_factory=lambda: int(os.getenv("TOKEN_TTL_MINUTES", "480")))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", False))

    # Optional AI layer. Without a key, column mapping falls back to a
    # deterministic matcher (aliases + fuzzy matching), so the app always runs.
    anthropic_api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY") or None)
    anthropic_model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"))
    use_llm: bool = field(default_factory=lambda: _bool("USE_LLM", True))

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key) and self.use_llm


settings = Settings()
