import json
import os
from pathlib import Path
_OAUTH_CACHE = Path.home() / '.cache/github-copilot-client/oauth.json'

def _device_flow_login():
    raise RuntimeError('Interactive login and credential writes are disabled')

def _get_oauth_token(*, allow_interactive_auth: bool = True) -> str:
    """Prefer the environment token, then the cached device OAuth login."""
    token = os.environ.get("COPILOT_GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        cached = json.loads(_OAUTH_CACHE.read_text(encoding="utf-8"))
        cached_token = (
            cached.get("oauth_token") if isinstance(cached, dict) else None
        )
        if isinstance(cached_token, str) and cached_token.strip():
            return cached_token.strip()
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        pass
    if not allow_interactive_auth:
        raise RuntimeError(
            "GitHub Copilot authentication is unavailable: "
            "COPILOT_GITHUB_TOKEN is empty and the existing OAuth cache is "
            "missing, unreadable, malformed, or contains no token; interactive "
            "device flow is disabled"
        )
    return _device_flow_login()

