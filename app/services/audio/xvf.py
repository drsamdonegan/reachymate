"""Single USB-control owner for XVF3800. Profiles are volatile, never saved."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

PROFILES = {
    "factory": {},
    "pollen_conv": {
        "PP_AGCMAXGAIN": [10],
        "PP_MIN_NS": [0.8],
        "PP_MIN_NN": [0.8],
        "PP_GAMMA_E": [0.5],
        "PP_GAMMA_ETAIL": [0.5],
        "PP_NLATTENONOFF": [0],
        "PP_MGSCALE": [4, 1, 1],
    },
    "door_v1": {"PP_MIN_NS": [0.10], "PP_MIN_NN": [0.51], "PP_ATTNS_MODE": [1], "PP_AGCMAXGAIN": [10]},
    "door_fixedbeam": {"AEC_FIXEDBEAMSONOFF": [1], "AEC_FIXEDBEAMSGATING": [1]},
}
REGISTERS = (
    "VERSION",
    "AUDIO_MGR_OP_L",
    "AUDIO_MGR_OP_R",
    "AEC_AECCONVERGED",
    "AEC_SPENERGY_VALUES",
    "PP_AGCMAXGAIN",
    "PP_MIN_NS",
    "PP_MIN_NN",
    "PP_ATTNS_MODE",
)


class XvfController:
    def __init__(self, *, profile: str = "factory", factory=None) -> None:
        if profile not in PROFILES:
            raise ValueError("Unknown XVF profile")
        self.profile = profile
        self.factory = factory
        self.lock = threading.RLock()
        self._doa: Any | None = None
        self._reboots: deque[float] = deque()
        self.registers: dict[str, Any] = {}
        self.last_error: str | None = None
        self._last_beam_update = 0.0
        self._beam_bearing: float | None = None

    def open(self) -> None:
        with self.lock:
            if self._doa is not None:
                return
            if self.factory is None:
                from reachy_mini.media.audio_doa import AudioDoA

                self.factory = AudioDoA
            self._doa = self.factory()
            if not self._doa.available:
                self.close()
                raise RuntimeError("XVF USB device 38fb:1001 unavailable; replug required")
            self.registers = {key: self._doa.respeaker.read(key) for key in REGISTERS}
            cfg = PROFILES[self.profile]
            if cfg:
                verified = self._doa.respeaker.apply_audio_config(tuple(cfg.items()), verify=True)
                if verified is not True:
                    raise RuntimeError("XVF profile verification failed")
            version = self.registers.get("VERSION")
            if not version or tuple(version[-3:]) < (2, 1, 4):
                self.last_error = "firmware_below_2.1.4"
            else:
                self.last_error = None

    def read_doa(self):
        with self.lock:
            return None if self._doa is None else self._doa.get_DoA()

    def steer_fixed_beam(self, bearing_radians: float, *, measured_better: bool = False) -> bool:
        """A/B option only after measured improvement over factory auto-select."""
        import math

        if self.profile != "door_fixedbeam" or not measured_better:
            return False
        with self.lock:
            now = time.monotonic()
            if self._doa is None or now - self._last_beam_update < 0.5:
                return False
            if self._beam_bearing is not None and abs(bearing_radians - self._beam_bearing) <= math.radians(
                5
            ):
                return False
            theta = max(0.0, min(math.pi, math.pi / 2 - bearing_radians))
            cfg = (("AEC_FIXEDBEAMSAZIMUTH_VALUES", (theta, math.pi / 2)),)
            if self._doa.respeaker.apply_audio_config(cfg, verify=True) is not True:
                raise RuntimeError("fixed_beam_verification_failed")
            self._last_beam_update, self._beam_bearing = now, bearing_radians
            return True

    def close(self) -> None:
        with self.lock:
            if self._doa is not None:
                self._doa.close()
                self._doa = None

    def reboot(self) -> None:
        """Caller closes CoreAudio first; recover at most three times per hour."""
        with self.lock:
            now = time.monotonic()
            while self._reboots and self._reboots[0] < now - 3600:
                self._reboots.popleft()
            if len(self._reboots) >= 3:
                raise RuntimeError("audio_reboot_limit")
            if self._doa is None:
                raise RuntimeError("XVF USB device unavailable")
            self._reboots.append(now)
            self._doa.respeaker.write("REBOOT", [1])
            self.close()
            time.sleep(4)
            self.open()
