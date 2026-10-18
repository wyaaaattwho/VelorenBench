from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import stat
import struct
import time
import subprocess
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from scripts.rpg_benchmark import lifecycle


def exception_detail(error: BaseException, seen: set[int] | None = None) -> dict:
    seen = set() if seen is None else seen
    if id(error) in seen:
        return {"type": type(error).__name__, "message": str(error), "recursive_reference": True}
    seen = seen | {id(error)}
    value = {"type": type(error).__name__, "message": str(error),
             "traceback": "".join(traceback.format_exception(error))}
    if isinstance(error, BaseExceptionGroup):
        value["exceptions"] = [exception_detail(child, seen) for child in error.exceptions]
    if error.__cause__ is not None:
        value["cause"] = exception_detail(error.__cause__, seen)
    elif error.__context__ is not None and not error.__suppress_context__:
        value["context"] = exception_detail(error.__context__, seen)
    return value


class NativeRefusal(RuntimeError):
    def __init__(self, command: str, error: dict):
        self.command, self.native_error = command, error
        super().__init__(f"native {command} refused: {error}")


class NativeActionFailure(RuntimeError):
    def __init__(self, kind: str, receipt: dict):
        self.receipt = receipt
        super().__init__(f"native {kind} did not complete: status={receipt.get('status')}, detail={receipt.get('detail')}")


@dataclass(frozen=True)
class NativeConfig:
    server: Path | None = None
    bridge: Path | None = None
    assets: Path | None = None
    root: Path | None = None
    seed: int = lifecycle.SEED
    small_world: bool = False
    private_oracle: bool = False
    resident_fixture: str | None = None

    def require_inputs(self) -> None:
        if any(value is None for value in (self.server, self.bridge, self.assets, self.root)):
            raise ValueError("native runtime is unconfigured; supply a validated runtime config")


class SocketEndpoint:
    """Keep sockets in private output even when its pathname exceeds AF_UNIX limits."""
    def __init__(self, directory: Path):
        if (not directory.is_absolute() or ".." in directory.parts
                or any(p.is_symlink() for p in (directory, *directory.parents))):
            raise ValueError("socket directory must be absolute without traversal or symlinks")
        self.descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.path = Path(f"/proc/{os.getpid()}/fd/{self.descriptor}") / f"rpg-{uuid4().hex}.sock"

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


@asynccontextmanager
async def private_server(out: Path, record: dict, config: NativeConfig):
    config.require_inputs()
    port, web_port = lifecycle.free_port(), lifecycle.free_port()
    while port == web_port or lifecycle.protected_ports().intersection((port, web_port)):
        port, web_port = lifecycle.free_port(), lifecycle.free_port()
    userdata = out / "userdata"
    settings = userdata / "server/server_config/settings.ron"
    settings.parent.mkdir(parents=True)
    map_setting = (f'Some(Save("{out / "engineering-map.bin"}",'
                   '(x_lg:7,y_lg:7,scale:1.0,erosion_quality:0.1)))'
                   if config.small_world else "None")
    settings.write_text(
        f'(gameserver_protocols: [Tcp(address: "127.0.0.1:{port}")], '
        f'query_address: None, auth_server_address: None, world_seed: {config.seed}, '
        f'map_file: {map_setting}, server_name: "Private RPG Benchmark",)\n')
    cli = userdata / "server-cli/settings.ron"
    cli.parent.mkdir(parents=True)
    cli.write_text(f'(web_address: "127.0.0.1:{web_port}")\n')
    environment = {**os.environ, "VELOREN_USERDATA": str(userdata),
                   "VELOREN_ASSETS": str(config.assets),
                   "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    environment.pop("VELOREN_PRIVATE_NATIVE_FIXTURE", None)
    environment.pop("VELOREN_PRIVATE_ORACLE_PATH", None)
    if config.private_oracle:
        environment["VELOREN_PRIVATE_ORACLE_PATH"] = str(out / "private-native-oracle.ndjson")
        environment["VELOREN_SESSION_ID"] = out.name
        record["private_oracle"] = {
            "schema": "rpg-native-oracle/1.1", "path": str(out / "private-native-oracle.ndjson"),
            "visibility": "server-owner-only; never public tools/model context"}
    if config.resident_fixture is not None:
        if not config.private_oracle or config.resident_fixture not in {
                "resident-smoke-v1", "outcomes-message-v1", "outcomes-slay-v1"}:
            raise ValueError("unsupported private native resident fixture")
        environment["VELOREN_PRIVATE_NATIVE_FIXTURE"] = config.resident_fixture
        record["resident_fixture"] = config.resident_fixture
    provision = await asyncio.to_thread(
        subprocess.run, [str(config.server), "--no-auth", "admin", "add",
                         record["username"], "admin"],
        cwd=config.root, env=environment, capture_output=True, timeout=30, check=False)
    record.update(private_port=port, private_web_port=web_port, world_seed=config.seed,
                  userdata=str(userdata), settings_sha256=lifecycle.sha(settings),
                  admin_provisioning={"returncode": provision.returncode})
    if provision.returncode:
        raise RuntimeError(f"private admin provisioning failed: {provision.returncode}")
    process = None
    try:
        with (out / "server.log").open("x") as stream:
            process = subprocess.Popen([str(config.server), "--no-auth", "--non-interactive"],
                                       cwd=config.root, env=environment, stdout=stream,
                                       stderr=subprocess.STDOUT)
            record.update(server_pid=process.pid,
                          server_start_ticks=lifecycle.process_start_ticks(process.pid))
            record["private_listener_inode"] = await lifecycle.wait_for_server(process, port)
            stream.flush()
            expected = f"Assets found path={config.assets}"
            if expected not in (out / "server.log").read_text():
                raise RuntimeError("native server did not select the pinned assets directory")
            yield port, userdata
    finally:
        if process is not None:
            if process.poll() is None:
                lifecycle.assert_owned(process.pid, record["server_start_ticks"])
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=15)
                except subprocess.TimeoutExpired:
                    lifecycle.assert_owned(process.pid, record["server_start_ticks"])
                    process.kill()
                    await asyncio.to_thread(process.wait)
            record.update(server_exit_code=process.returncode,
                          server_stopped=process.poll() is not None)


class Journal:
    def __init__(self, path: Path):
        self.stream = path.open("x", encoding="utf-8")

    def write(self, **row: object) -> None:
        self.stream.write(json.dumps({"wall_monotonic": time.monotonic(), **row},
                                     allow_nan=False) + "\n")
        self.stream.flush()

    def close(self) -> None:
        self.stream.close()


async def signal_owned(process: asyncio.subprocess.Process, start: int, sig: int) -> bool:
    """Signal a verified live child, or reap an already vanished owned child."""
    if process.returncode is not None:
        return False
    try:
        lifecycle.assert_owned(process.pid, start)
    except FileNotFoundError as error:
        # Only absence of this child's stat is an exit race. Missing protection
        # metadata, permission errors and live/recycled identity refusal propagate.
        if error.filename != f"/proc/{process.pid}/stat":
            raise
        await process.wait()
        return False
    try:
        process.send_signal(sig)
    except ProcessLookupError:
        await process.wait()
        return False
    return True


async def stop_owned(process: asyncio.subprocess.Process, start: int, record: dict) -> None:
    if process.returncode is None:
        await signal_owned(process, start, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 15)
        except TimeoutError:
            await signal_owned(process, start, signal.SIGKILL)
            await process.wait()
    record.update(pid=process.pid, start_ticks=start, exit_code=process.returncode,
                  stopped=process.returncode is not None)


class NativeBridge:
    """One real bridge with concurrent, ID-correlated requests and durable receipts."""

    def __init__(self, out: Path, name: str, port: int, userdata: Path, journal: Journal,
                 config: NativeConfig | None = None):
        self.out, self.name, self.port = out, name, port
        self.userdata, self.journal = userdata, journal
        self.pending: dict[str, asyncio.Future] = {}
        self.pending_requests: dict[str, tuple[dict, str]] = {}
        self.ready: asyncio.Future | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task | None = None
        self.cleanup: dict = {}
        self.measured_receipts: list[dict] = []
        self.stderr = None
        self.closing = False
        self.config = config or NativeConfig()
        self.native_events: list[dict] = []
        self.error_observations: list[dict] = []
        self.effect_observations: dict[str, dict] = {}
        self.failed: Exception | None = None
        self.submitted_ids: set[str] = set()

    async def start(self) -> None:
        self.config.require_inputs()
        environment = os.environ.copy()
        environment.update(
            VELOREN_SERVER=f"127.0.0.1:{self.port}", VELOREN_USERNAME=self.name,
            VELOREN_PASSWORD="", VELOREN_CHARACTER=self.name,
            VELOREN_SESSION_ID=self.name, VELOREN_ASSETS=str(self.config.assets),
            VELOREN_USERDATA=str(self.userdata), NO_PROXY="127.0.0.1,localhost",
            no_proxy="127.0.0.1,localhost",
        )
        self.stderr = (self.out / f"{self.name}-bridge.log").open("x")
        self.ready = asyncio.get_running_loop().create_future()
        self.process = await asyncio.create_subprocess_exec(
            str(self.config.bridge), cwd=self.config.root, env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=self.stderr, limit=16 * 1024 * 1024,
        )
        self.start_ticks = lifecycle.process_start_ticks(self.process.pid)
        self.reader_task = asyncio.create_task(self._read())
        await asyncio.wait_for(asyncio.shield(self.ready), 90)

    async def _read(self) -> None:
        try:
            while line := await self.process.stdout.readline():
                row = json.loads(line)
                if row.get("type") == "event":
                    self.native_events.append(row)
                    self.journal.write(origin="native_event", actor=self.name, event=row)
                    self.on_event(row)
                if row.get("event") == "ready" and not self.ready.done():
                    self.ready.set_result(row)
                elif row.get("type") == "response":
                    request_id = row["id"]
                    future = self.pending.get(request_id)
                    if future is None:
                        raise RuntimeError(f"unknown or duplicate native response ID: {request_id}")
                    if (row.get("protocol_version") != self.ready.result()["protocol_version"]
                            or row.get("session_id") != self.ready.result()["session_id"]):
                        raise RuntimeError("native response session/protocol changed")
                    request, origin = self.pending_requests.pop(request_id)
                    self.pending.pop(request_id)
                    self.journal.write(origin=origin, actor=self.name, response=row)
                    if request_id in self.effect_observations:
                        self.effect_observations[request_id].update(
                            effect_status="known", response=row, receipt_monotonic=time.monotonic())
                    if origin in {"agent", "mechanism"} and request["cmd"] in {"step", "set_control_state", "release_control_state"}:
                        self.measured_receipts.append(self.measured_receipt(request, row))
                    if not future.done():
                        future.set_result(row)
                elif row.get("event") == "fatal":
                    raise RuntimeError(f"native bridge fatal: {row}")
            if self.pending:
                raise RuntimeError(
                    f"native bridge closed without terminal receipts: {sorted(self.pending)}; effects unknown")
            if not self.closing:
                raise RuntimeError(f"native bridge exited: {self.name}")
        except Exception as error:
            self.failed = error
            self.error_observations.append({
                "source": "native_reader", "phase": "cleanup" if self.closing else "measurement",
                "wall_monotonic": time.monotonic(), "error": exception_detail(error)})
            self.journal.write(origin="infrastructure", actor=self.name,
                               error=exception_detail(error),
                               unresolved_requests=[request for request, _ in self.pending_requests.values()])
            if not self.ready.done():
                self.ready.set_exception(error)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()
            self.pending_requests.clear()

    def on_event(self, row: dict) -> None:
        pass

    def measured_receipt(self, request: dict, response: dict) -> dict:
        return {"request": request, "response": response, "actor": self.ready.result()["uid"],
                "wall_monotonic": time.monotonic()}

    async def envelope(self, request: dict, *, origin: str, timeout: float | None = 60) -> dict:
        if self.failed is not None:
            raise RuntimeError("native bridge reader failed; no further requests submitted") from self.failed
        if self.closing and origin == "agent":
            raise RuntimeError("native bridge closing; agent request not submitted")
        request_id = request["id"]
        if request_id in self.pending or request_id in self.submitted_ids:
            raise ValueError("duplicate native request ID; mutation retry is forbidden")
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        self.pending[request_id] = future
        self.pending_requests[request_id] = (request, origin)
        self.journal.write(origin=origin, actor=self.name, request=request)
        mutation = request["cmd"] in CONTROL_COMMANDS
        if mutation:
            self.effect_observations[request_id] = {
                "request": request, "origin": origin, "actor": self.ready.result()["uid"],
                "session_id": self.ready.result()["session_id"], "effect_status": "not_submitted"}
        write_started = False
        try:
            payload = (json.dumps(request) + "\n").encode()
            write_started = True
            self.submitted_ids.add(request_id)
            if mutation:
                self.effect_observations[request_id]["effect_status"] = "unknown"
            self.process.stdin.write(payload)
            await self.process.stdin.drain()
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except BaseException as error:
            self.error_observations.append({
                "source": "native_request", "phase": "cleanup" if origin == "cleanup" else origin,
                "wall_monotonic": time.monotonic(), "error": exception_detail(error),
                "request_id": request_id, "effect_status": "unknown" if write_started else "not_submitted"})
            self.journal.write(origin="infrastructure", actor=self.name,
                               request_error=exception_detail(error), request=request,
                               effect_status="unknown" if write_started else "not_submitted")
            if not write_started:
                self.pending.pop(request_id, None)
                self.pending_requests.pop(request_id, None)
                future.cancel()
            raise

    async def request(self, command: str, args: dict | None = None, *, origin: str = "setup") -> dict:
        response = await self.envelope({
            "id": uuid4().hex, "protocol_version": self.ready.result()["protocol_version"],
            "cmd": command, "args": args or {},
        }, origin=origin)
        if response.get("ok") is not True:
            error = response.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                raise RuntimeError("native refusal envelope lacks structured error detail")
            raise NativeRefusal(command, error)
        return response["result"]

    async def close(self) -> None:
        self.closing = True
        try:
            if self.process is not None and self.process.returncode is None and self.ready.done() and not self.ready.exception():
                reads = {identifier: future for identifier, future in self.pending.items()
                         if self.pending_requests[identifier][0]["cmd"] in READ_COMMANDS | {"frame", "ui"}}
                if reads:
                    self.journal.write(origin="cleanup", actor=self.name,
                                       draining_original_reads=list(reads))
                    # Close acknowledges session retirement, not completion of GPU readback.
                    # Retain the existing 60-second native read-operation timeout.
                    await asyncio.wait_for(asyncio.gather(
                        *(asyncio.shield(future) for future in reads.values())), 60)
                    self.journal.write(origin="cleanup", actor=self.name,
                                       original_reads_drained=list(reads))
                await self.request("close", origin="cleanup")
        finally:
            if self.process is not None:
                await stop_owned(self.process, self.start_ticks, self.cleanup)
            if self.reader_task is not None:
                await self.reader_task
            if self.stderr is not None:
                self.stderr.close()


READ_COMMANDS = {
    "observe", "events", "capabilities", "inspect_terrain", "inspect_map",
    "goal_recipes", "action_status", "health", "ping",
}
CONTROL_COMMANDS = {"step", "cancel", "acquire_control", "set_control_state", "release_control_state"}


def allowed(request: dict) -> bool:
    command = request.get("cmd")
    if command in READ_COMMANDS or command in CONTROL_COMMANDS - {"step"} or command == "close":
        return True
    action = request.get("args", {}).get("action", {})
    return command == "step" and action.get("type") != "server_command" and not (
        action.get("type") == "chat" and str(action.get("message", "")).lstrip().startswith("/")
    )


class EpisodeBoundary:
    """One attachable native player; judge queries never become agent observations."""

    def __init__(self, bridge: NativeBridge, socket_path: Path):
        self.bridge, self.path = bridge, socket_path
        self.attached = False
        self.server = None
        self.errors: list[str] = []
        self.error_details: list[dict] = []
        self.connections: set[asyncio.Task] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.peer: dict = {}
        self.closing = False
        self.socket_identity: tuple[int, int] | None = None

    def _error(self, error: Exception) -> None:
        self.errors.append(f"{type(error).__name__}: {error}")
        self.error_details.append({
            **exception_detail(error), "wall_monotonic": time.monotonic(),
            "phase": "cleanup" if self.closing else "measurement"})
        self.bridge.journal.write(origin="infrastructure", boundary_error=self.errors[-1],
                                  boundary_error_detail=self.error_details[-1])

    def begin_close(self) -> None:
        self.closing = True
        if self.server is not None:
            self.server.close()
        for writer in list(self.writers):
            writer.close()

    async def start(self) -> None:
        if self.closing or self.server is not None:
            raise RuntimeError("episode boundary already started or closing")
        self.server = await asyncio.start_unix_server(self._connect, path=str(self.path),
                                                     limit=16 * 1024 * 1024,
                                                     cleanup_socket=False)
        info = self.path.lstat()
        self.socket_identity = (info.st_dev, info.st_ino)
        self.path.chmod(0o600)

    async def _connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.connections.add(task)
        self.writers.add(writer)
        tasks: dict[asyncio.Task, dict] = {}

        def peer_loss(event: str, error: OSError | None = None, **detail) -> None:
            self.bridge.journal.write(origin="boundary_transport", event=event,
                                      error=exception_detail(error) if error else None,
                                      pending_request_ids=[request.get("id") for request in tasks.values()],
                                      **detail)

        async def dispatch(request: dict) -> None:
            if request.get("cmd") == "close":
                response = {"type": "response", "id": request["id"], "ok": True,
                            "protocol_version": self.bridge.ready.result()["protocol_version"],
                            "session_id": self.bridge.ready.result()["session_id"], "result": {}}
            elif not self.request_allowed(request):
                response = {"type": "response", "id": request["id"], "ok": False,
                            "protocol_version": self.bridge.ready.result()["protocol_version"],
                            "session_id": self.bridge.ready.result()["session_id"],
                            "error": {"code": "benchmark_admin_forbidden",
                                      "message": "Environment administration is unavailable during measurement."}}
                self.bridge.journal.write(origin="agent", rejected_request=request, response=response)
            else:
                options = {"timeout": None} if request["cmd"] in CONTROL_COMMANDS else {}
                response = await self.bridge.envelope(request, origin="agent", **options)
            try:
                writer.write((json.dumps(self.public_response(response, request)) + "\n").encode())
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                peer_loss("known_response_not_delivered", error, request=request, response=response)

        try:
            if self.attached:
                raise RuntimeError("episode already has an actuator owner")
            pid, uid, _ = struct.unpack(
                "3i", writer.get_extra_info("socket").getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if uid != os.getuid():
                raise RuntimeError("episode attachment belongs to another OS user")
            self.peer = {"pid": pid, "start_ticks": lifecycle.process_start_ticks(pid)}
            self.attached = True
            try:
                writer.write((json.dumps(self.public_ready()) + "\n").encode())
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                peer_loss("ready_peer_disconnected", error)
                return
            async with asyncio.TaskGroup() as group:
                while not self.closing:
                    try:
                        line = await reader.readline()
                    except (BrokenPipeError, ConnectionResetError) as error:
                        peer_loss("peer_read_disconnected", error)
                        break
                    if not line:
                        peer_loss("peer_eof")
                        break
                    if self.closing:
                        break
                    request = json.loads(line)
                    child = group.create_task(dispatch(request))
                    tasks[child] = request
                    child.add_done_callback(lambda done: tasks.pop(done, None))
        except Exception as error:
            self._error(error)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError) as error:
                peer_loss("peer_close_disconnected", error)
            except Exception as error:
                self._error(error)
            finally:
                self.writers.discard(writer)
                self.connections.discard(task)

    def public_ready(self) -> dict:
        return self.bridge.ready.result()

    def request_allowed(self, request: dict) -> bool:
        return allowed(request)

    def public_response(self, response: dict, request: dict) -> dict:
        return response

    async def close(self) -> None:
        self.begin_close()
        if self.server is not None:
            await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        if self.connections:
            await asyncio.gather(*self.connections)
        if self.socket_identity is not None:
            try:
                info = self.path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (not stat.S_ISSOCK(info.st_mode)
                        or (info.st_dev, info.st_ino) != self.socket_identity):
                    raise RuntimeError("episode socket identity changed; refusing to unlink")
                self.path.unlink()
            self.socket_identity = None
        if self.peer:
            try:
                current = lifecycle.process_start_ticks(self.peer["pid"])
            except FileNotFoundError:
                self.peer["retired"] = True
            else:
                self.peer["retired"] = current != self.peer["start_ticks"]
