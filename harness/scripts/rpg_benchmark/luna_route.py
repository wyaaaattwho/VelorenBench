"""Existing authorized Luna route; credentials only through its existing loader."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen

HEADERS = {
    "User-Agent": "GitHubCopilotChat/0.35.0", "Editor-Version": "vscode/1.107.0",
    "Editor-Plugin-Version": "copilot-chat/0.35.0", "Copilot-Integration-Id": "vscode-chat",
}


class ExistingLunaRoute:
    def __init__(self, loader: Path, digest: str):
        if (not loader.is_absolute() or any(p.is_symlink() for p in (loader, *loader.parents))
                or hashlib.sha256(loader.read_bytes()).hexdigest() != digest):
            raise ValueError("existing credential loader identity mismatch")
        spec = importlib.util.spec_from_file_location("_rpg_existing_credential_loader", loader)
        if spec is None or spec.loader is None:
            raise ValueError("existing credential loader cannot be imported")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.load = module._get_oauth_token
        self.access, self.expires, self.base = None, 0, None

    def __call__(self):
        try:
            if time.time() >= self.expires:
                oauth = self.load(allow_interactive_auth=False)
                headers = {**HEADERS, "Accept": "application/json",
                           "Authorization": "token " + oauth}
                with urlopen(Request("https://api.github.com/copilot_internal/v2/token",
                                     headers=headers), timeout=30) as response:
                    value = json.load(response)
                oauth = None
                token, expiry = value.get("token"), value.get("expires_at")
                if not isinstance(token, str) or not token or type(expiry) not in (int, float):
                    raise RuntimeError("invalid credential exchange fields")
                match = re.search(r"proxy-ep=([^;]+)", token)
                host = (match.group(1).replace("proxy.", "api.", 1) if match
                        else "api.individual.githubcopilot.com")
                if not re.fullmatch(r"api\.(?:individual|business|enterprise)\.githubcopilot\.com", host):
                    raise RuntimeError("existing account route is outside the authorized official endpoint")
                self.access, self.expires, self.base = token, expiry - 300, f"https://{host}"
            return self.base + "/responses", {
                **HEADERS, "Content-Type": "application/json", "Authorization": "Bearer " + self.access,
                "X-Initiator": "user", "Openai-Intent": "conversation-edits"}
        except (OSError, ValueError, RuntimeError, KeyError):
            raise RuntimeError("existing authorized credential/route unavailable; details withheld") from None

    def close(self):
        self.access, self.load = None, None
