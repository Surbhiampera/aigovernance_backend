"""Application configuration loaded from environment variables."""
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))


def get_log_level() -> str:
    return os.getenv("LOG_LEVEL", "INFO").upper()


def get_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "*")
    if raw.strip() == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def get_lookup_defaults(name: str) -> list[str]:
    """Return injected lookup defaults from env vars (no hardcoded enums in source).

    Format: env var ``LOOKUP_<NAME>`` is a comma-separated list, e.g.
    ``LOOKUP_AUTH_TYPES="API Key,OAuth,Basic Auth"``.
    Anything not configured returns an empty list — DB values still drive the
    dropdown.
    """
    env_key = f"LOOKUP_{name.upper().replace('-', '_')}"
    raw = os.getenv(env_key, "")
    return [item.strip() for item in raw.split(",") if item and item.strip()]
