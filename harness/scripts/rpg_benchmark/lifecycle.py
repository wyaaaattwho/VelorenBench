"""Linux process ownership and local artifact helpers; no provider dependencies."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import time

SEED = 130626853
_extra_guards: ContextVar[tuple] = ContextVar("rpg_process_guards", default=())


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8")


def process_start_ticks(pid: int) -> int:
    content = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    return int(content.rsplit(") ", 1)[1].split()[19])


def guards() -> tuple[dict, ...]:
    host = json.loads(Path(__file__).with_name("host-protection.json").read_text())
    builtins = tuple(host["processes"]) if host["hostname"] == platform.node() else ()
    return builtins + _extra_guards.get()


def configure_guards(processes: list[dict]) -> None:
    if not isinstance(processes, list):
        raise ValueError("protected_processes must be a list")
    for row in processes:
        if (not isinstance(row, dict) or set(row) != {"pid", "start_ticks", "port"}
                or any(type(row[k]) is not int or row[k] <= 0 for k in row)
                or row["port"] > 65535):
            raise ValueError("protected process requires positive integer pid/start_ticks/port")
    _extra_guards.set(tuple(processes))


def protected_ports() -> set[int]:
    return {row["port"] for row in guards()}


def assert_owned(pid: int, start: int) -> None:
    if any(row["pid"] == pid for row in guards()):
        raise RuntimeError(f"refusing to signal protected PID {pid}")
    if type(pid) is not int or type(start) is not int or process_start_ticks(pid) != start:
        raise RuntimeError("PID identity changed; refusing to signal")


def listener_inodes(port: int) -> list[str]:
    inodes = []
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        with open(name, encoding="ascii") as stream:
            next(stream)
            for line in stream:
                fields = line.split()
                if int(fields[1].split(":")[1], 16) == port and fields[3] == "0A":
                    inodes.append(fields[9])
    return sorted(inodes)


def protection_snapshot() -> list[dict]:
    result = []
    for row in guards():
        start = process_start_ticks(row["pid"])
        if start != row["start_ticks"]:
            raise RuntimeError(f"protected PID identity changed: {row['pid']}")
        result.append({**row, "listener_inodes": listener_inodes(row["port"])})
    return result


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def owned_listener_inode(process: subprocess.Popen, port: int) -> str:
    if process.poll() is not None:
        raise RuntimeError(f"private server exited during readiness: {process.returncode}")
    listener = []
    with open("/proc/net/tcp", encoding="ascii") as stream:
        next(stream)
        for line in stream:
            fields = line.split()
            if (fields[3] == "0A" and int(fields[1].split(":")[1], 16) == port
                    and fields[1].split(":")[0] == "0100007F"):
                listener.append(fields[9])
    descriptors = {os.readlink(path) for path in Path(f"/proc/{process.pid}/fd").iterdir()
                   if path.is_symlink()}
    owned = [inode for inode in listener if f"socket:[{inode}]" in descriptors]
    if len(listener) != 1 or len(owned) != 1:
        raise RuntimeError("private server did not exclusively own the selected loopback port")
    return owned[0]


async def wait_for_server(process: subprocess.Popen, port: int) -> str:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"private server exited during startup: {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return owned_listener_inode(process, port)
        except OSError:
            await asyncio.sleep(0.25)
    raise TimeoutError("private server did not bind selected port")
