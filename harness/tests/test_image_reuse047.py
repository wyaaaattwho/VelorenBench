import asyncio
import base64
import copy
import hashlib
from unittest.mock import patch

import pytest

from scripts.rpg_benchmark import responses
from .test_multimodal import Evidence
from .test_task_runtime import configuration, declaration


def item(data):
    return {"role": "user", "content": [{"type": "input_image", "image_url":
        "data:image/png;base64," + base64.b64encode(data).decode("ascii")}]}


def test_reuse_checks_exact_bytes_and_counts_every_wire_block():
    cache = {}
    one, two = item(b"one"), item(b"two")
    with patch.object(responses.base64, "b64decode", wraps=base64.b64decode) as decode:
        first = responses.image_identities([one], cache=cache)
        second = responses.image_identities(copy.deepcopy([one, one, two]), cache=cache)
    assert decode.call_count == 2
    assert len(second) == 3
    assert second[0] == second[1] == first[0]
    assert second[2]["sha256"] == hashlib.sha256(b"two").hexdigest()
    second[0]["sha256"] = "changed"
    assert responses.image_identities([one], cache=cache) == first


def test_cache_never_accepts_a_changed_or_malformed_image_by_position():
    cache = {}
    responses.image_identities([item(b"one")], cache=cache)
    changed = item(b"two")
    assert responses.image_identities([changed], cache=cache)[0]["sha256"] == hashlib.sha256(b"two").hexdigest()
    changed["content"][0]["image_url"] += "!"
    with pytest.raises(ValueError):
        responses.image_identities([changed], cache=cache)


def test_actual_send_preserves_full_history_and_repeated_sent_acknowledged_counts():
    evidence = Evidence()
    provider = responses.ResponsesProvider(evidence, declaration=declaration(),
        configuration=configuration(), mock_url="http://127.0.0.1:1234")
    bodies = []
    def post(body):
        bodies.append(copy.deepcopy(body))
        return {"id": str(len(bodies)), "status": "completed", "output": [{
            "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "observed"}]}]}
    provider._post = post
    history = [{"role": "user", "content": "original task"}, item(b"one")]
    asyncio.run(provider.send(provider.body(history)))
    history.extend([{"type": "function_call_output", "call_id": "c", "output": "{}"}, item(b"one"), item(b"two")])
    asyncio.run(provider.send(provider.body(history)))
    assert bodies[1]["input"] == history
    assert bodies[0]["input"] == history[:2]
    sent = [row for row in evidence.rows if row["kind"] == "provider_request"]
    acked = [row for row in evidence.rows if row["kind"] == "provider_response"]
    assert [row["actual_image_blocks"] for row in sent] == [1, 3]
    assert [len(row["images"]) for row in acked] == [1, 3]
    assert [row["images"] for row in sent] == [row["images"] for row in acked]
