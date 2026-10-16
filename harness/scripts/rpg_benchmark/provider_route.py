"""Explicit standard Responses endpoint; no account discovery or provider fallback."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit


class StandardResponsesRoute:
    def __init__(self, endpoint: str, api_key_env: str):
        url = urlsplit(endpoint)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or not url.path.endswith("/responses")):
            raise ValueError("Standard provider endpoint must be an explicit HTTPS /responses URL")
        if not isinstance(api_key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", api_key_env):
            raise ValueError("Select an environment-variable name, never a credential in configuration")
        if not os.environ.get(api_key_env, "").strip():
            raise ValueError(f"Provider credential environment variable is missing: {api_key_env}")
        self.endpoint, self.api_key_env = endpoint, api_key_env

    def __call__(self):
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise RuntimeError("Selected provider credential is no longer available")
        return self.endpoint, {"Content-Type": "application/json", "Authorization": "Bearer " + key}

    def close(self):
        pass
