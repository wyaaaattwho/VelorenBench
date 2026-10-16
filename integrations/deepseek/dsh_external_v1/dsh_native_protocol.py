import importlib
import os
import hashlib
from pathlib import Path

common = importlib.import_module("scripts.rpg_sdk_agent")
expected = os.environ.get("RPG_COMMON_INTERFACE_SHA256")
if expected is not None and hashlib.sha256(Path(common.__file__).read_bytes()).hexdigest() != expected:
    raise ValueError("Installed common player interface differs from evaluator content")

Evidence = common.Evidence
NativeClient = common.NativeClient
NativeError = common.NativeError
error_record = common.error_record
