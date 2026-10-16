from __future__ import annotations

import asyncio
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .controller import VelorenController
from .errors import BridgeCommandError, BridgeTimeoutError, VelorenControllerError
from .telemetry import TELEMETRY_SCHEMA_VERSION, TelemetryCollector


WEB_API_VERSION = "0.1"
STATIC_DIRECTORY = Path(__file__).with_name("web_static")


class ActionRequest(BaseModel):
    action: dict[str, Any]
    action_id: str | None = Field(default=None, min_length=1, max_length=128)
    duration_ticks: int | None = Field(default=None, ge=1, le=3_600)
    timeout_ticks: int | None = Field(default=None, ge=1, le=36_000)

    @field_validator("action")
    @classmethod
    def validate_action(cls, action: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(action.get("type"), str) or not action["type"]:
            raise ValueError("action.type must be a non-empty string")
        return action


class RecordingRequest(BaseModel):
    filename: str | None = Field(default=None, max_length=128)
    append: bool = False

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, filename: str | None) -> str | None:
        if filename is None:
            return None
        if Path(filename).name != filename or not filename.endswith(".jsonl"):
            raise ValueError("filename must be a local .jsonl filename")
        return filename


class ControllerRuntime:
    """Owns one controller and telemetry collector for one web process."""

    def __init__(
        self,
        controller_factory: Callable[[], VelorenController] = VelorenController,
        *,
        sample_hz: float = 10.0,
        recording_directory: str | Path | None = None,
    ) -> None:
        self._controller_factory = controller_factory
        self._sample_hz = sample_hz
        self.recording_directory = (
            Path(recording_directory)
            if recording_directory is not None
            else Path.cwd() / "recordings"
        )
        self._lock = threading.RLock()
        self.controller: VelorenController | None = None
        self.telemetry: TelemetryCollector | None = None
        self.started_at: float | None = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return (
                self.controller is not None
                and self.controller.is_running
                and self.telemetry is not None
                and self.telemetry.is_running
            )

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self.recording_directory.mkdir(parents=True, exist_ok=True)
            controller = self._controller_factory()
            controller.start()
            telemetry = TelemetryCollector(controller, sample_hz=self._sample_hz)
            try:
                telemetry.start()
            except Exception:
                controller.close()
                raise
            self.controller = controller
            self.telemetry = telemetry
            self.started_at = time.time()

    def stop(self) -> None:
        with self._lock:
            telemetry, self.telemetry = self.telemetry, None
            controller, self.controller = self.controller, None
            self.started_at = None
        try:
            if telemetry is not None:
                telemetry.stop()
        finally:
            if controller is not None:
                controller.close()

    def require_controller(self) -> VelorenController:
        with self._lock:
            controller = self.controller
        if controller is None or not controller.is_running:
            raise RuntimeError("Veloren controller is not running")
        return controller

    def require_telemetry(self) -> TelemetryCollector:
        with self._lock:
            telemetry = self.telemetry
        if telemetry is None or not telemetry.is_running:
            raise RuntimeError("telemetry collector is not running")
        return telemetry

    def health(self) -> dict[str, Any]:
        with self._lock:
            controller = self.controller
            telemetry = self.telemetry
            return {
                "web_api_version": WEB_API_VERSION,
                "status": "running" if self.is_running else "stopped",
                "controller_running": bool(controller and controller.is_running),
                "telemetry_running": bool(telemetry and telemetry.is_running),
                "session_id": controller.session_id if controller else None,
                "started_at": self.started_at,
            }

    def recording_path(self, filename: str | None) -> Path:
        actual = filename or f"telemetry-{int(time.time())}.jsonl"
        return self.recording_directory / actual


class TelemetryHub:
    """Fans one telemetry queue out to independent WebSocket queues."""

    def __init__(self, queue_limit: int = 32) -> None:
        self.queue_limit = queue_limit
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self.dropped_messages = 0

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        target: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=self.queue_limit
        )
        async with self._lock:
            self._subscribers.add(target)
        return target

    async def unsubscribe(self, target: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._subscribers.discard(target)

    async def publish(self, message: dict[str, Any]) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers)
        for target in subscribers:
            if target.full():
                try:
                    target.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self.dropped_messages += 1
            target.put_nowait(message)

    async def run(self, runtime: ControllerRuntime) -> None:
        while runtime.is_running:
            telemetry = runtime.require_telemetry()
            try:
                message = await asyncio.to_thread(telemetry.get_message, 1.0)
            except BridgeTimeoutError:
                continue
            await self.publish(message)


def create_app(
    runtime: ControllerRuntime | None = None,
    *,
    auto_start: bool = True,
    control_token: str | None = None,
) -> FastAPI:
    actual_runtime = runtime or ControllerRuntime()
    hub = TelemetryHub()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        hub_task: asyncio.Task[None] | None = None
        if auto_start:
            await asyncio.to_thread(actual_runtime.start)
        if actual_runtime.is_running:
            hub_task = asyncio.create_task(hub.run(actual_runtime))
        try:
            yield
        finally:
            if hub_task is not None:
                hub_task.cancel()
                await asyncio.gather(hub_task, return_exceptions=True)
            if auto_start:
                await asyncio.to_thread(actual_runtime.stop)

    app = FastAPI(
        title="Veloren Agent Visualization API",
        version=WEB_API_VERSION,
        lifespan=lifespan,
    )
    app.state.runtime = actual_runtime
    app.state.telemetry_hub = hub
    app.mount("/static", StaticFiles(directory=STATIC_DIRECTORY), name="static")

    @app.middleware("http")
    async def security_headers(
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "connect-src 'self' ws: wss:; "
            "img-src 'self' data:; "
            "script-src 'self'; "
            "style-src 'self'; "
            "style-src-attr 'unsafe-inline'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api/") or request.url.path.startswith(
            "/static/"
        ) or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIRECTORY / "index.html")

    def require_control(
        x_agent_token: str | None = Header(default=None),
    ) -> None:
        if control_token is not None and not secrets.compare_digest(
            x_agent_token or "", control_token
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "invalid_control_token"},
            )

    def running_telemetry() -> TelemetryCollector:
        try:
            return actual_runtime.require_telemetry()
        except RuntimeError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "runtime_not_running", "message": str(error)},
            ) from error

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        value = actual_runtime.health()
        value["websocket_dropped_messages"] = hub.dropped_messages
        return value

    @app.get("/api/state")
    async def state_snapshot(
        telemetry: TelemetryCollector = Depends(running_telemetry),
    ) -> dict[str, Any]:
        return telemetry.snapshot()

    @app.post("/api/action", dependencies=[Depends(require_control)])
    async def execute_action(request: ActionRequest) -> dict[str, Any]:
        try:
            controller = actual_runtime.require_controller()
            result = await asyncio.to_thread(
                controller.step,
                request.action,
                action_id=request.action_id,
                duration_ticks=request.duration_ticks,
                timeout_ticks=request.timeout_ticks,
            )
        except BridgeCommandError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": error.code, "message": error.message},
            ) from error
        except BridgeTimeoutError as error:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail={"code": "bridge_timeout", "message": str(error)},
            ) from error
        except (RuntimeError, VelorenControllerError) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "controller_unavailable", "message": str(error)},
            ) from error
        return asdict(result)

    @app.post("/api/recording/start", dependencies=[Depends(require_control)])
    async def start_recording(
        request: RecordingRequest,
        telemetry: TelemetryCollector = Depends(running_telemetry),
    ) -> dict[str, Any]:
        path = actual_runtime.recording_path(request.filename)
        try:
            telemetry.start_recording(path, append=request.append)
        except RuntimeError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "recording_active", "message": str(error)},
            ) from error
        return {"recording": True, "path": str(path)}

    @app.post("/api/recording/stop", dependencies=[Depends(require_control)])
    async def stop_recording(
        telemetry: TelemetryCollector = Depends(running_telemetry),
    ) -> dict[str, Any]:
        telemetry.stop_recording()
        return {"recording": False}

    @app.websocket("/ws")
    async def telemetry_socket(websocket: WebSocket) -> None:
        try:
            telemetry = actual_runtime.require_telemetry()
        except RuntimeError:
            await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
            return
        await websocket.accept()
        target = await hub.subscribe()
        try:
            await websocket.send_json(
                {
                    "type": "telemetry_snapshot",
                    "telemetry_schema_version": TELEMETRY_SCHEMA_VERSION,
                    "snapshot": telemetry.snapshot(),
                }
            )
            while True:
                try:
                    message = await asyncio.wait_for(target.get(), timeout=15.0)
                except TimeoutError:
                    message = {
                        "type": "heartbeat",
                        "captured_at": time.time(),
                        "session_id": telemetry.controller.session_id,
                    }
                await websocket.send_json(message)
        except WebSocketDisconnect:
            pass
        finally:
            await hub.unsubscribe(target)

    return app
