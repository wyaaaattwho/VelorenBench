from __future__ import annotations

import argparse
import asyncio
import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
from uuid import uuid4

from dsh_external_v1.dsh_native_protocol import Evidence, NativeClient, error_record
from dsh_external_v1.dsh_provider import CopilotProtocolProxy
from dsh_external_v1.dsh_tools import PlayerTools, ToolBroker
from dsh_external_v1.dsh_timing import stamp

ROOT = Path(__file__).resolve().parent
PROFILE = "native-game"
MODEL = "gpt-6-luna"
PROVIDER = "github-copilot"
EFFORT = "off"  # Official pi-ai's catalog maps off to none; the proxy enforces the wire.
PERSONA = (
    "You are playing a native game. Pursue the user's goal using only the provided player tools. "
    "Choose your own stages, targets, movement and operations. Read current player observations. "
    "Native coordinates are Z-up [x,y,z]. A submission or applied input is not a verified effect "
    "or task success. Keep action identities and ownership fences. Never retry an unknown effect "
    "with a new identity. No administrator commands or slash chat are available."
)


def write_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("x") as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def start_ticks(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])


def identity_retired(pid: int, ticks: int) -> bool:
    try:
        text = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return True
    return int(text[19]) != ticks or text[0] == "Z"


def identity_gone(pid: int, ticks: int) -> bool:
    try:
        return start_ticks(pid) != ticks
    except FileNotFoundError:
        return True


def own_orphaned_children() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # Linux PR_SET_CHILD_SUBREAPER.
        raise OSError(ctypes.get_errno(), "Unable to own orphaned runtime children")


def profile_tree(home: Path, broker: Path, provider_url: str, *, persona: str = PERSONA) -> list[dict]:
    def row(identifier, package, **config):
        return {"id": identifier, "name": f"@deepseek-ai/{package}",
                **({"config": config} if config else {})}
    return [
        row("sdk-app", "dsh-sdk-app", profile=PROFILE),
        row("sdk-server", "dsh-sdk-jsonrpc-server", maxTokensAsSuccess=False),
        row("timer", "cordis-plugin-timer"),
        row("llm", "dsh-llm"),
        row("pi", "dsh-llm-pi-ai", providers={PROVIDER: {
            "apiKeyEnv": "DSH_LOCAL_PROVIDER_KEY", "baseURL": provider_url,
            "models": [{"id": MODEL}],
        }}),
        row("session", "dsh-session"),
        row("projections", "dsh-session-projection"),
        row("fs", "dsh-fs-local"),
        row("working-directory", "dsh-working-directory"),
        row("system-prompt", "dsh-system-prompt", includeHarnessIdentity=False,
            includeRuntimeContext=False, personaPrefix=persona),
        row("tools", "dsh-tools"),
        row("attachments", "dsh-attachment-local"),
        row("agent", "dsh-agent"),
        row("llm-retry", "dsh-llm-retry"),
        row("jobs", "dsh-jobs-local"),
        row("agent-loop", "dsh-agent-loop", agents=[]),
        row("sessions", "dsh-session-persistence-jsonl", compression="none",
            root=str(home / "sessions")),
        {"id": "player-tools", "name": str(ROOT / "game-plugin.mjs"),
         "config": {"socket": str(broker)}},
    ]


def prepare_profile(home: Path, broker: Path, url: str, *, persona: str = PERSONA, session_id: str) -> None:
    profile = home / "profiles" / PROFILE
    profile.mkdir(parents=True, exist_ok=False)
    tree = profile_tree(home, broker, url, persona=persona)
    tree[-1]["config"]["sessionId"] = session_id
    dependencies = {row["name"]: "0.2.1-alpha.3" for row in tree
                    if row["name"].startswith("@deepseek-ai/dsh-")}
    dependencies["@deepseek-ai/cordis-plugin-timer"] = "1.1.7-alpha.2"
    write_json(profile / "package.json", {
        "name": "dsh-profile-native-game", "private": True, "dependencies": dependencies,
        "dsh": {"profile": {"bundles": []}},
    })
    (profile / "cordis.patch.yml").write_text(json.dumps([{"insert": tree}], indent=2) + "\n")


def child_environment(home: Path, key: str, runtime: Path) -> dict:
    result = {name: os.environ[name] for name in ("PATH", "LANG", "TZ")
              if name in os.environ}
    result.update(DSH_HOME=str(home), HOME=str(home), DO_NOT_TRACK="1",
                  DSH_LOCAL_PROVIDER_KEY=key, NO_PROXY="127.0.0.1,localhost",
                  DSH_RUNTIME=str(runtime),
                  no_proxy="127.0.0.1,localhost")
    return result


@dataclass
class _RPCRequest:
    method: str
    future: asyncio.Future
    sent: bool = False
    response_received: bool = False
    cancellation: dict | None = None


class OfficialRuntime:
    """Stdio SDK projection of the actual official dsh CLI and Cordis composition."""

    def __init__(self, out: Path, evidence: Evidence, runtime: Path):
        self.out, self.evidence = out, evidence
        self.runtime = runtime
        self.process = None
        self.pending = {}
        # Keep ownership until transport retirement, including consumed replies,
        # so cancellation permits exactly one late response, never a duplicate.
        self.rpc_requests: dict[str, _RPCRequest] = {}
        self.shutdown_received = False
        self.turns = {}
        self.reader = None
        self.tracker = None
        self.owned: dict[int, int] = {}
        self.stderr = None
        self.cleanup = {}
        self.closing = False
        self.transport_failure = None

    async def start(self, home: Path, key: str) -> None:
        own_orphaned_children()
        work = self.out / "work"
        work.mkdir()
        self.stderr = (self.out / "dsh-stderr.log").open("x")
        entry = stamp()
        self.process = await asyncio.create_subprocess_exec(
            str(self.runtime / "node_modules/.bin/dsh"), "--profile", PROFILE,
            cwd=work, env=child_environment(home, key, self.runtime),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=self.stderr, limit=16 * 1024 * 1024)
        write_json(self.out / "harness-entry.json", entry)
        self.owned[self.process.pid] = start_ticks(self.process.pid)
        self.evidence.write("official_runtime_launch", pid=self.process.pid,
                            start_ticks=self.owned[self.process.pid], entry=entry)
        self.reader = asyncio.create_task(self._read())
        self.tracker = asyncio.create_task(self._track())

    async def _track(self) -> None:
        while self.process.returncode is None:
            self._discover()
            await asyncio.sleep(.02)

    def _discover(self) -> None:
        queue = list(self.owned)
        while queue:
            parent = queue.pop()
            if identity_retired(parent, self.owned[parent]):
                continue
            children = Path(f"/proc/{parent}/task/{parent}/children")
            try:
                pids = children.read_text().split()
            except FileNotFoundError:
                continue
            for raw in pids:
                pid = int(raw)
                if pid not in self.owned:
                    try:
                        self.owned[pid] = start_ticks(pid)
                    except FileNotFoundError:
                        continue
                    queue.append(pid)
                    self.evidence.write("owned_child", pid=pid, start_ticks=self.owned[pid])

    async def _read(self) -> None:
        try:
            while line := await self.process.stdout.readline():
                row = json.loads(line)
                self.evidence.write("dsh_jsonrpc", envelope=row)
                if row.get("method") == "session.event":
                    params = row["params"]
                    if params["event"]["type"] == "turn/end":
                        self.turns[params["sessionId"]] = params["event"]["data"]["reason"]
                if "id" in row:
                    identifier = row["id"]
                    request = self.rpc_requests.get(identifier)
                    if request is None or not request.sent or request.response_received:
                        self.evidence.write(
                            "dsh_rpc_response", request_id=identifier,
                            method=request.method if request is not None else None,
                            disposition="duplicate" if request is not None and
                            request.response_received else "unknown")
                        raise RuntimeError("unknown/duplicate official runtime response ID")
                    future = request.future
                    # Task.cancel() can cancel the awaited future before rpc's
                    # except block gets CPU time. Preserve that same cause here.
                    if future.cancelled() and request.cancellation is None:
                        try:
                            future.result()
                        except asyncio.CancelledError as error:
                            self._cancel_rpc(identifier, error)
                    self.evidence.write(
                        "dsh_rpc_response", request_id=identifier, method=request.method,
                        disposition="cancelled" if request.cancellation is not None else "active")
                    if "result" not in row and "error" not in row:
                        raise RuntimeError("official runtime response lacks result/error")
                    request.response_received = True
                    if request.cancellation is not None:
                        # Only successful replies are neutral. A real remote
                        # failure must not disappear with its cancelled caller.
                        if "error" in row:
                            raise RuntimeError(json.dumps(row["error"]))
                        self.evidence.write(
                            "dsh_rpc_late_cancelled_response", request_id=identifier,
                            method=request.method, cause=request.cancellation)
                        continue
                    if future.done():
                        raise RuntimeError("unknown/duplicate official runtime response ID")
                    if "error" in row:
                        future.set_exception(RuntimeError(json.dumps(row["error"])))
                    else:
                        future.set_result(row["result"])
                        if request.method == "shutdown":
                            self.shutdown_received = True
            if (self.closing and self.shutdown_received
                    and all(future.done() for future in self.pending.values())):
                return
            raise RuntimeError("official dsh runtime stdout closed")
        except (RuntimeError, ValueError, OSError) as error:
            self.transport_failure = error
            self.evidence.write("dsh_transport_error", error=error_record(error))
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)

    def _cancel_rpc(self, identifier: str, error: asyncio.CancelledError) -> None:
        request = self.rpc_requests[identifier]
        if request.cancellation is None:
            request.cancellation = error_record(error)
            self.evidence.write(
                "dsh_rpc_cancelled", request_id=identifier, method=request.method,
                cause=request.cancellation, response_received=request.response_received)
        if not request.future.done():
            request.future.cancel(str(error))

    async def rpc(self, method: str, params: dict) -> dict:
        if self.transport_failure is not None:
            raise self.transport_failure
        identifier = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        request = self.rpc_requests[identifier] = _RPCRequest(method, future)
        try:
            self.process.stdin.write((json.dumps({
                "jsonrpc": "2.0", "id": identifier, "method": method, "params": params,
            }) + "\n").encode())
            request.sent = True
            self.evidence.write("dsh_rpc_request", request_id=identifier, method=method)
            await self.process.stdin.drain()
            return await future
        except asyncio.CancelledError as error:
            self._cancel_rpc(identifier, error)
            raise
        finally:
            self.pending.pop(identifier, None)

    def finished_turn(self, session: str) -> dict:
        reason = self.turns.get(session)
        if not isinstance(reason, dict) or not isinstance(reason.get("kind"), str):
            raise RuntimeError("official dsh idle lacks a valid durable turn/end receipt")
        if reason["kind"] != "completed":
            raise RuntimeError("official dsh turn failed: " + json.dumps(reason))
        return reason

    async def close(self) -> None:
        if self.cleanup:
            return
        if self.process is None:
            if self.stderr is not None:
                self.stderr.close()
            self.cleanup = {"stopped": True, "started": False}
            return
        self.closing = True
        self._discover()
        if self.process.returncode is None:
            try:
                await asyncio.wait_for(self.rpc("shutdown", {}), 2)
                await asyncio.wait_for(self.process.wait(), 2)
            except (RuntimeError, OSError, TimeoutError) as error:
                self.evidence.write("runtime_shutdown_error", error=error_record(error))
                if not identity_retired(self.process.pid, self.owned[self.process.pid]):
                    self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except TimeoutError:
                    if not identity_retired(self.process.pid, self.owned[self.process.pid]):
                        self.process.kill()
                    await self.process.wait()
        for task in (self.tracker, self.reader):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        for pid, ticks in self.owned.items():
            if not identity_retired(pid, ticks):
                os.kill(pid, signal.SIGTERM)
        await asyncio.sleep(.05)
        for pid, ticks in self.owned.items():
            if not identity_retired(pid, ticks):
                os.kill(pid, signal.SIGKILL)
        for _ in range(100):
            for pid, ticks in self.owned.items():
                if pid != self.process.pid and not identity_gone(pid, ticks):
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError as error:
                        self.evidence.write("child_not_reapable", pid=pid,
                                            error_type=type(error).__name__)
            if all(identity_gone(pid, ticks) for pid, ticks in self.owned.items()):
                break
            await asyncio.sleep(.02)
        self.cleanup = {
            "stopped": all(identity_gone(pid, ticks) for pid, ticks in self.owned.items()),
            "exit_code": self.process.returncode,
            "owned_processes": [{"pid": pid, "start_ticks": ticks,
                                 "retired": identity_retired(pid, ticks),
                                 "identity_gone_or_reaped": identity_gone(pid, ticks)}
                                for pid, ticks in self.owned.items()],
            "cleanup_end": stamp(),
        }
        self.stderr.close()
        if not self.cleanup["stopped"]:
            raise RuntimeError("official runtime child retirement incomplete")
