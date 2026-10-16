import os
from pathlib import Path

import uvicorn

from veloren_controller.web import ControllerRuntime, create_app


HOST = os.environ.get("VELOREN_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("VELOREN_WEB_PORT", "8765"))
CONTROL_TOKEN = os.environ.get("VELOREN_WEB_CONTROL_TOKEN") or None
SAMPLE_HZ = float(os.environ.get("VELOREN_TELEMETRY_HZ", "10"))
RECORDING_DIRECTORY = Path(
    os.environ.get(
        "VELOREN_RECORDING_DIRECTORY",
        str(Path(__file__).resolve().parent / "recordings"),
    )
)

if HOST not in {"127.0.0.1", "::1", "localhost"} and CONTROL_TOKEN is None:
    raise RuntimeError(
        "VELOREN_WEB_CONTROL_TOKEN is required when binding outside localhost"
    )

runtime = ControllerRuntime(
    sample_hz=SAMPLE_HZ,
    recording_directory=RECORDING_DIRECTORY,
)
app = create_app(runtime, control_token=CONTROL_TOKEN)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, workers=1)
