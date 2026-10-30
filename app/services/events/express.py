"""An explicit, restart-safe handoff of Reachy's camera to Luma's browser scanner."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from app.services.events.qr import EVENT_ID


class LumaExpressHandoff:
    def __init__(self, state_path: Path | None = None):
        self.path = state_path
        self.active = bool(state_path and state_path.exists())
        self.ready = False
        self.event_id = None
        if self.active:
            try:
                if state_path.stat().st_size > 1024:
                    raise ValueError()
                value = json.loads(state_path.read_text())
                if EVENT_ID.fullmatch(value.get("event_id", "")):
                    self.event_id = value["event_id"]
            except (OSError, ValueError, TypeError, AttributeError):
                pass  # An unreadable marker must keep automatic camera/voice startup paused.

    def begin(self, event_id: str) -> None:
        if not EVENT_ID.fullmatch(event_id):
            raise ValueError("Select a valid Luma event first")
        if self.active and event_id != self.event_id:
            raise ValueError("Close the current Luma scanner and return the camera before changing events")
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as file:
                    temporary = Path(file.name)
                    json.dump({"event_id": event_id}, file)
                os.replace(temporary, self.path)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        self.active, self.ready, self.event_id = True, False, event_id

    def finish(self) -> None:
        if self.path:
            self.path.unlink(missing_ok=True)
        self.active, self.ready, self.event_id = False, False, None

    def snapshot(self) -> dict:
        return {
            "active": self.active,
            "camera_released": self.ready,
            # Browser camera permission, camera selection and Express Mode are controlled by Luma/Chrome.
            "url": f"https://luma.com/check-in/{self.event_id}/scan" if self.event_id else None,
            "requires_browser_setup": True,
        }
