"""Deterministic synthetic JSON-RPC peer; never a native game or provider."""
import json
import sys


def send(identifier, **payload):
    print(json.dumps({"jsonrpc": "2.0", "id": identifier, **payload}), flush=True)


scenario = sys.argv[1]
wait = json.loads(sys.stdin.readline())
assert wait["method"] == "session/wait"
print(json.dumps({"jsonrpc": "2.0", "method": "test.wait_accepted"}), flush=True)
shutdown = json.loads(sys.stdin.readline())
assert shutdown["method"] == "shutdown"
if scenario == "wait-first":
    send(wait["id"], result={})
    send(shutdown["id"], result={"shutdown": "ack"})
elif scenario == "shutdown-first":
    send(shutdown["id"], result={"shutdown": "ack"})
    send(wait["id"], result={})
elif scenario == "duplicate-cancelled":
    send(wait["id"], result={})
    send(wait["id"], result={})
    send(shutdown["id"], result={"shutdown": "ack"})
elif scenario == "unknown":
    send("never-sent-id", result={})
    send(shutdown["id"], result={"shutdown": "ack"})
elif scenario == "duplicate-active":
    send(shutdown["id"], result={"shutdown": "ack"})
    send(shutdown["id"], result={"shutdown": "ack"})
elif scenario == "cancelled-error":
    send(wait["id"], error={"code": -32000, "message": "synthetic provider failure"})
    send(shutdown["id"], result={"shutdown": "ack"})
elif scenario == "active-error":
    send(shutdown["id"], error={"code": -32000, "message": "synthetic shutdown failure"})
elif scenario == "pipe-closure":
    pass
else:
    raise AssertionError(scenario)
