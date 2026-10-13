"""Fail-closed audio-visual admission of speech, with bounded RAM pre-roll."""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class GateResult:
    chunks: tuple[bytes, ...]
    opened: bool
    reason: str
    degraded: bool


class AVGate:
    def __init__(
        self,
        *,
        mode: str = "demo",
        doa_validated: bool = False,
        sample_rate: int = 16000,
        attack_seconds: float = 0.16,
        hangover_seconds: float = 0.48,
        pre_roll_seconds: float = 0.32,
        zero_tail_seconds: float | None = None,
        dither_validated: bool = False,
    ) -> None:
        if mode not in {"demo", "pilot"}:
            raise ValueError("Unknown gate mode")
        self.mode, self.doa_validated, self.sample_rate = mode, doa_validated, sample_rate
        self.dither_validated = dither_validated
        self.zero_tail_seconds = zero_tail_seconds
        self._closed_since: float | None = None
        self.attack, self.hangover, self.pre_roll = attack_seconds, hangover_seconds, pre_roll_seconds
        self._ring: deque[tuple[float, bytes]] = deque()
        self._candidate: float | None = None
        self._last_eligible: float | None = None
        self._bearing_agrees = False
        self._open = False
        self._track = None
        self._veto_until = 0.0
        self._behind = 0

    def reset(self) -> None:
        self._ring.clear()
        self._candidate = self._last_eligible = None
        self._open = self._bearing_agrees = False
        self._track = None
        self._behind = 0
        self._veto_until = 0.0
        self._closed_since = None

    @property
    def is_open(self) -> bool:
        return self._open

    def dither_verdict(
        self,
        *,
        before_theta: float,
        after_theta: float,
        yaw_delta: float,
        face_silent: bool,
        settled_seconds: float,
        now: float | None = None,
    ) -> str:
        now = time.monotonic() if now is None else now
        if not face_silent or abs(yaw_delta) < math.radians(6) or settled_seconds < 0.6:
            return "inconclusive"
        change = (math.cos(after_theta) - math.cos(before_theta)) * math.copysign(1, yaw_delta)
        if abs(change) < 0.02:
            return "inconclusive"
        if change > 0:
            self._behind += 1
            if self._behind >= 2:
                self._veto_until = now + 5
            return "behind"
        self._behind = 0
        return "front"

    def process(
        self, chunk: bytes, *, vad_score: float, face=None, doa=None, now: float | None = None
    ) -> GateResult:
        now = time.monotonic() if now is None else now
        silence = b"\0" * len(chunk)
        degraded = not self.doa_validated or doa is None or now - doa.timestamp > 0.2
        face_ok = face is not None and face.in_zone and 0 <= now - face.timestamp < 2
        if face is not None and face.track_id != self._track:
            if self._track is not None:
                self.reset()
            self._track = face.track_id
        chip_speech = doa is not None and 0 <= now - doa.timestamp <= 0.2 and doa.speech
        speech = vad_score >= 0.5 or chip_speech
        bearing_ok = True
        if face_ok and not degraded:
            if face.bearing is None:
                bearing_ok = False
            else:
                delta = abs(doa.bearing - face.bearing)
                if delta <= math.radians(20):
                    self._bearing_agrees = True
                elif delta >= math.radians(30):
                    self._bearing_agrees = False
                bearing_ok = self._bearing_agrees
        veto = now < self._veto_until
        mouth_ok = True
        if self.mode == "pilot":
            mouth_ok = bool(face_ok and face.mouth_active is True)
            if mouth_ok is False:
                degraded = degraded or face is None or face.mouth_active is None
            if (
                face_ok
                and chip_speech
                and abs(doa.bearing) <= math.radians(15)
                and face.mouth_still_seconds >= 0.6
            ):
                veto = True
        hard_closed = not face_ok or not bearing_ok or veto or not mouth_ok
        eligible = speech and not hard_closed
        was_open = self._open
        duration = len(chunk) / (2 * self.sample_rate)
        self._ring.append((now, chunk))
        while self._ring and now - self._ring[0][0] >= self.pre_roll:
            self._ring.popleft()
        if hard_closed:
            self._candidate = None
            self._last_eligible = None
            self._open = False
            self._ring.clear()
        elif eligible:
            if self._candidate is None:
                self._candidate = now - duration
            self._last_eligible = now
            self._open = now - self._candidate >= self.attack - 1e-6
        elif self._last_eligible is None or now - self._last_eligible >= self.hangover:
            self._candidate = None
            self._open = False
        focus_degraded = degraded or self.mode == "pilot" and not self.dither_validated
        if self._open:
            self._closed_since = None
            output = tuple(value for _, value in self._ring) if not was_open else (chunk,)
            self._ring.clear()
            return GateResult(output, True, "visitor_speech", focus_degraded)
        reason = (
            "no_engaged_face"
            if not face_ok
            else "bearing_mismatch"
            if not bearing_ok
            else "rear_veto"
            if veto
            else "mouth_unavailable_or_still"
            if not mouth_ok
            else "speech_attack"
        )
        self._closed_since = now if self._closed_since is None else self._closed_since
        output = (
            ()
            if self.zero_tail_seconds is not None and now - self._closed_since >= self.zero_tail_seconds
            else (silence,)
        )
        return GateResult(output, False, reason, focus_degraded)


class MouthActivity:
    """MediaPipe mouth aspect ratio only; no embeddings or frame retention."""

    def __init__(self, model_path: str) -> None:
        import mediapipe as mp

        self.mp = mp
        opts = mp.tasks.vision.FaceLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=model_path), num_faces=1
        )
        self.model = mp.tasks.vision.FaceLandmarker.create_from_options(opts)
        self.values: deque[tuple[float, float]] = deque()
        self._track_bbox = None
        self._last_active = time.monotonic()

    def process(self, frame, bbox) -> tuple[bool | None, float]:
        import cv2

        x, y, w, h = bbox
        crop = frame[max(0, y) : max(0, y) + h, max(0, x) : max(0, x) + w]
        if not crop.size:
            return None, 0
        result = self.model.detect(
            self.mp.Image(
                image_format=self.mp.ImageFormat.SRGB,
                data=np.ascontiguousarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)),
            )
        )
        if not result.face_landmarks:
            return None, 0
        landmarks = result.face_landmarks[0]

        def point(i):
            return np.array((landmarks[i].x * w, landmarks[i].y * h))

        width = np.linalg.norm(point(61) - point(291))
        mar = float(np.linalg.norm(point(13) - point(14)) / max(width, 1e-6))
        now = time.monotonic()
        from app.services.vision.attention import intersection_over_union

        if self._track_bbox is not None and intersection_over_union(self._track_bbox, bbox) < 0.3:
            self.values.clear()
            self._last_active = now
        self._track_bbox = bbox
        self.values.append((now, mar))
        while self.values and self.values[0][0] < now - 0.4:
            self.values.popleft()
        if len(self.values) < 3 or now - self.values[0][0] < 0.3:
            return None, 0
        ratios = np.array([v for _, v in self.values])
        active = bool(ratios.std() >= 0.035 or np.ptp(ratios) >= 0.10)
        if active:
            self._last_active = now
        return active, now - self._last_active

    def close(self) -> None:
        self.model.close()
