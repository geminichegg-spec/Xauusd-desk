"""API key loading for the desk. Keys come from environment variables only.

Order: a real environment variable (e.g. a GitHub Actions secret) wins,
otherwise the value from a local .env file next to this script.
Keys are never printed, logged, or written anywhere.
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))

PROVIDERS = {
    "gemini": {"key": "GEMINI_API_KEY", "model": "GEMINI_MODEL", "default_model": "gemini-3.8-flash"},
    "grok": {"key": "XAI_API_KEY", "model": "GROK_MODEL", "default_model": "grok-4"},
}


def load_dotenv(path: str | None = None) -> None:
    """Read KEY=VALUE lines from .env into os.environ without overriding real env vars."""
    path = path or os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v


def get_key(provider: str) -> str:
    p = PROVIDERS[provider]
    key = os.environ.get(p["key"], "").strip()
    if not key:
        raise RuntimeError(
            f"{p['key']} is not set. Add it to your .env file (copy .env.example) "
            f"or as a GitHub Actions secret named {p['key']}."
        )
    return key


def get_model(provider: str) -> str:
    p = PROVIDERS[provider]
    return os.environ.get(p["model"], "").strip() or p["default_model"]


def status() -> dict:
    """Which keys are present (True/False only, never the key itself)."""
    return {name: bool(os.environ.get(p["key"], "").strip()) for name, p in PROVIDERS.items()}


if __name__ == "__main__":
    load_dotenv()
    for name, ok in status().items():
        print(f"{name:7} {'key found' if ok else 'missing'}  model={get_model(name)}")
