"""Local operator kill switch: a flag or a marker file always forces D0."""

import threading
from pathlib import Path


class KillSwitch:
    def __init__(self, marker: Path | None = None) -> None:
        self.marker = marker
        self._active = threading.Event()

    def activate(self) -> None:
        self._active.set()

    def clear(self) -> None:
        self._active.clear()

    @property
    def active(self) -> bool:
        return self._active.is_set() or (self.marker is not None and self.marker.exists())
