"""Explicit opt-in for unchanged regressions over non-distributed local data."""
import hashlib
import os
from pathlib import Path

import pytest

EXPECTED = {
    "accepted-contracts.json": "c6a0a8108853d27d29c6692c7b88667da90e15d707937c35005a7824bc29b8ed",
    "native-contract-traces.json": "82e56a2a7a4a0eb917654d901dc111eb8d01c13971ce7710533fd11dc33a15f1",
    "scorer-identity-052.json": "82e1b8d8275551d20243542378bb36adb7228a18c49db1cccebbeeaed9fa83f5",
    "stability-repair.json": "4ede22ffcd0137a655ae71a89aef2d3731c8a133477a9e6de2a25c973d74f2b9",
}


def historical_fixture(name):
    root = os.environ.get("VELORENBENCH_HISTORICAL_FIXTURES")
    if not root:
        pytest.skip("optional non-distributed historical fixture; see TESTING.md: " + name)
    path = Path(root) / name
    # A selected but unavailable/altered fixture is an error, never a skip.
    assert hashlib.sha256(path.read_bytes()).hexdigest() == EXPECTED[name], name
    return path
