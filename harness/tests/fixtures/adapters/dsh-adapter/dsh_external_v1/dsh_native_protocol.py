import importlib
import os
from pathlib import Path

common = importlib.import_module("scripts.rpg_sdk_agent")
expected = Path(os.environ["RPG_COMMON_INTERFACE"]).resolve()
if Path(common.__file__).resolve() != expected:
    raise ValueError("Imported common player interface does not match benchmark context")

Evidence = common.Evidence
NativeClient = common.NativeClient
NativeError = common.NativeError
error_record = common.error_record
