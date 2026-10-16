class VelorenControllerError(RuntimeError):
    """Base error raised by the Python controller."""


class BridgeStartupError(VelorenControllerError):
    """The Rust bridge failed before becoming ready."""


class BridgeTimeoutError(VelorenControllerError):
    """A bridge request did not complete before its deadline."""


class BridgeCommandError(VelorenControllerError):
    """The bridge rejected a command."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
