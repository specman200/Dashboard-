"""Common interface every robot data source implements."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..model import Snapshot

# Maps the numeric run-state some controllers/PLCs expose onto dashboard states.
# Override per installation with [source.state_codes] in the config file.
DEFAULT_STATE_CODES = {
    0: "stopped",
    1: "idle",
    2: "running",
    3: "teach",
    4: "alarm",
    5: "emergency",
}


class DataSource(ABC):
    """Reads one :class:`Snapshot` from the robot each time ``read`` is called."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.state_codes = {
            int(k): v for k, v in config.get("state_codes", DEFAULT_STATE_CODES).items()
        }

    def connect(self) -> None:  # pragma: no cover - optional hook
        """Open the connection. Called before the first read and after failures."""

    def close(self) -> None:  # pragma: no cover - optional hook
        """Release the connection."""

    @abstractmethod
    def read(self) -> Snapshot:
        """Return the latest reading. Raise on communication errors."""

    def decode_state(self, raw) -> str | None:
        if raw is None:
            return None
        if isinstance(raw, str) and not raw.lstrip("-").isdigit():
            return raw.lower()
        return self.state_codes.get(int(raw), "idle")
