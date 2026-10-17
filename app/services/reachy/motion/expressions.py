"""Local, pinned Pollen trajectories and bounded all-axis motion sequences."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.services.reachy.motion.commands import RobotMotionRequest

LIBRARIES = {
    "dances": ("pollen-robotics/reachy-mini-dances-library", "3564295e72d41c1271f46bf5540a3fcad9d5f669"),
    "emotions": ("pollen-robotics/reachy-mini-emotions-library", "873ae49f0b89114b7e535eff0c1f7560d21d9357"),
}
AXES = [
    "head_x",
    "head_y",
    "head_z",
    "head_roll",
    "head_pitch",
    "head_yaw",
    "body_yaw",
    "right_antenna",
    "left_antenna",
]


# SDK 1.11 assets/config/hardware_config.yaml, after motor homing offsets.
# Position registers use 4096 ticks per turn, centred at 2048.
_STEWART_MIN = (np.array([1502, 1138, 1502, 1138, 1252, 1138]) - 2048) * math.pi / 2048
_STEWART_MAX = (np.array([2958, 2844, 2958, 2594, 2958, 2594]) - 2048) * math.pi / 2048


def reachable_joints(joints) -> bool:
    values = np.asarray(joints, dtype=float)
    return bool(
        values.shape == (7,)
        and np.isfinite(values).all()
        and abs(values[0]) <= math.radians(160)
        and np.all(values[1:] >= _STEWART_MIN + math.radians(1))
        and np.all(values[1:] <= _STEWART_MAX - math.radians(1))
    )


@dataclass
class Frame:
    head: np.ndarray
    antennas: np.ndarray
    body: float

    @classmethod
    def neutral(cls):
        return cls(np.eye(4), np.radians([-10.0, 10.0]), 0.0)

    def copy(self):
        return Frame(self.head.copy(), self.antennas.copy(), self.body)


def blend(start: Frame, end: Frame, fraction: float) -> Frame:
    from reachy_mini.utils.interpolation import linear_pose_interpolation

    alpha = float(np.clip(fraction, 0, 1))
    # Minimum jerk avoids a step in commanded velocity at segment boundaries.
    alpha = 10 * alpha**3 - 15 * alpha**4 + 6 * alpha**5
    return Frame(
        linear_pose_interpolation(start.head, end.head, alpha),
        start.antennas + alpha * (end.antennas - start.antennas),
        start.body + alpha * (end.body - start.body),
    )


class MotionSequence:
    def __init__(self, frames: list[tuple[float, Frame]], *, label: str):
        self.frames, self.label = frames, label
        self.amplitude_scale = 1.0
        self.duration = frames[-1][0]
        if not 0 < self.duration <= 24:
            raise ValueError("Motion must finish within 24 seconds")

    def evaluate(self, elapsed: float) -> Frame:
        previous_time, previous = self.frames[0]
        for end_time, end in self.frames[1:]:
            if elapsed <= end_time:
                return blend(previous, end, (elapsed - previous_time) / max(end_time - previous_time, 0.001))
            previous_time, previous = end_time, end
        return previous.copy()

    def scaled(self, amplitude: float) -> MotionSequence:
        from reachy_mini.utils.interpolation import linear_pose_interpolation

        neutral = Frame.neutral()
        frames = [self.frames[0]]
        for timestamp, frame in self.frames[1:]:
            frames.append(
                (
                    timestamp,
                    Frame(
                        linear_pose_interpolation(neutral.head, frame.head, amplitude),
                        neutral.antennas + amplitude * (frame.antennas - neutral.antennas),
                        amplitude * frame.body,
                    ),
                )
            )
        result = MotionSequence(frames, label=self.label)
        result.amplitude_scale = amplitude
        return result


class MotionLibrary:
    def __init__(self, directory: Path):
        self.directory = directory
        self.catalog: dict[str, dict[str, dict]] = {kind: {} for kind in LIBRARIES}
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            return
        manifest = json.loads(manifest_path.read_text())
        for kind, (repo, revision) in LIBRARIES.items():
            entry = manifest.get(kind, {})
            if entry.get("repo") != repo or entry.get("revision") != revision:
                continue
            for name, digest in entry.get("files", {}).items():
                if Path(name).name != name or not name.endswith(".json"):
                    continue
                path = directory / kind / name
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    continue
                try:
                    data = json.loads(path.read_text())
                    self._validate_recording(data)
                except (ValueError, KeyError, TypeError):
                    continue
                self.catalog[kind][path.stem] = data

    @staticmethod
    def _validate_recording(data):
        times, frames = data["time"], data["set_target_data"]
        if not 2 <= len(times) == len(frames) <= 2000:
            raise ValueError("Invalid recording size")
        clock = np.asarray(times, dtype=float)
        if not np.isfinite(clock).all() or clock[0] < 0 or clock[-1] > 40 or np.any(np.diff(clock) < 0):
            raise ValueError("Invalid recording timeline")
        for entry in frames:
            head, antennas = np.asarray(entry["head"]), np.asarray(entry["antennas"])
            if head.shape != (4, 4) or antennas.shape != (2,):
                raise ValueError("Invalid recording frame")
            if not np.isfinite(head).all() or not np.isfinite(antennas).all():
                raise ValueError("Non-finite frame")
            if not math.isfinite(float(entry.get("body_yaw", 0))):
                raise ValueError("Non-finite body yaw")

    def capabilities(self) -> dict:
        return {
            "axes": AXES,
            "look_directions": ["left", "right", "up", "down", "forward", "around"],
            "gestures": ["nod", "shake", "wiggle_antennas", "neutral"],
            "dances": sorted(self.catalog["dances"]),
            "emotions": sorted(self.catalog["emotions"]),
            "max_duration_seconds": 20,
            "limits": {
                "body_yaw_degrees": [-160, 160],
                "relative_head_yaw_degrees": [-65, 65],
                "antenna_degrees": [-175, 175],
                "head_rotation_degrees_per_second": 80,
                "body_degrees_per_second": 60,
                "translation_mm_per_second": 60,
            },
            "recordings": "Pinned Apache-2.0 Pollen motion data; no sidecar audio is played.",
        }

    def sequence(self, request: RobotMotionRequest, start: Frame) -> MotionSequence:
        from reachy_mini.utils import create_head_pose
        from reachy_mini.utils.rotation import Rotation

        duration = request.duration_seconds
        label = request.name or request.direction
        frames = [(0.0, start.copy())]

        def pose(**kwargs):
            return Frame(create_head_pose(**kwargs), start.antennas.copy(), start.body)

        if request.action == "look":
            angles = {"left": (0, 30), "right": (0, -30), "up": (-15, 0), "down": (15, 0), "forward": (0, 0)}
            if request.direction == "around":
                for portion, yaw in [(0.25, 35), (0.6, -35), (1.0, 0)]:
                    frames.append((duration * portion, pose(yaw=math.degrees(start.body) + yaw)))
            else:
                pitch, yaw = angles[request.direction]
                end = pose(pitch=pitch, yaw=math.degrees(start.body) + yaw)
                frames += [(min(1.0, duration / 2), end), (duration, end)]
        elif request.action == "pose":
            rpy = Rotation.from_matrix(start.head[:3, :3]).as_euler("xyz", degrees=True)
            body = start.body if request.body_yaw_degrees is None else math.radians(request.body_yaw_degrees)
            yaw = rpy[2] if request.body_yaw_degrees is None else math.degrees(body)
            values = {}
            for index, axis in enumerate(("x", "y", "z")):
                value = getattr(request, axis + "_mm")
                values[axis] = start.head[index, 3] if value is None else value / 1000
            for index, axis in enumerate(("roll", "pitch", "yaw")):
                value = getattr(request, axis + "_degrees")
                values[axis] = (yaw if axis == "yaw" else rpy[index]) if value is None else value
            antennas = start.antennas.copy()
            for index, axis in enumerate(("right", "left")):
                value = getattr(request, axis + "_antenna_degrees")
                if value is not None:
                    antennas[index] = math.radians(value)
            end = Frame(create_head_pose(**values), antennas, body)
            angle = np.linalg.norm(Rotation.from_matrix(start.head[:3, :3].T @ end.head[:3, :3]).as_rotvec())
            duration = max(
                duration,
                1.9 * angle / math.radians(80),
                1.9 * abs(body - start.body) / math.radians(60),
                1.9 * float(np.max(np.abs(antennas - start.antennas))) / math.pi,
                1.9 * float(np.linalg.norm(end.head[:3, 3] - start.head[:3, 3])) / 0.06,
            )
            frames += [(duration, end)]
        elif request.action == "gesture":
            if request.name == "neutral":
                frames += [(duration, Frame.neutral())]
            elif request.name in {"nod", "shake", "wiggle_antennas"}:
                for index in range(1, 5):
                    frame = start.copy()
                    sign = 1 if index % 2 else -1
                    if request.name == "wiggle_antennas":
                        frame.antennas = np.radians([sign * 40, -sign * 40])
                    else:
                        offset = create_head_pose(**{"pitch" if request.name == "nod" else "yaw": sign * 12})
                        frame.head = start.head @ offset
                    frames.append((duration * index / 5, frame))
                frames.append((duration, start.copy()))
            else:
                raise ValueError("Unknown gesture; request capabilities for the catalogue")
        elif request.action in {"dance", "emotion"}:
            kind = "dances" if request.action == "dance" else "emotions"
            name = request.name
            if not name or name == "random":
                name = "groovy_sway_and_roll" if kind == "dances" else "cheerful1"
            data = self.catalog[kind].get(name)
            if data is None:
                raise ValueError("Move unavailable; request capabilities for the installed catalogue")
            label = name
            from reachy_mini.motion.recorded_move import RecordedMove

            move = RecordedMove(data)
            length = min(float(data["time"][-1]) - 0.001, duration / request.repeat)
            cursor = 0.6
            for _ in range(request.repeat):
                for t in np.arange(0, length, 0.02):
                    head, antennas, body = move.evaluate(float(t))
                    frames.append((cursor + float(t), Frame(head, antennas, float(body))))
                cursor += length
            frames.append((cursor + 0.8, Frame.neutral()))
        else:
            raise ValueError("This action does not start a movement")
        return MotionSequence(frames, label=f"{request.action}:{label}")


def safe_frame(frame: Frame, previous: Frame, dt: float) -> Frame:
    """Bound every actuator and slew rate, including recorded trajectories."""
    from reachy_mini.utils.rotation import Rotation

    head, antennas, body = frame.head.copy(), frame.antennas.copy(), frame.body
    if head.shape != (4, 4) or antennas.shape != (2,) or not np.isfinite(head).all():
        raise ValueError("Invalid movement frame")
    if not np.isfinite(antennas).all() or not math.isfinite(body):
        raise ValueError("Non-finite movement frame")
    body = float(np.clip(body, -math.radians(160), math.radians(160)))
    roll, pitch, yaw = Rotation.from_matrix(head[:3, :3]).as_euler("xyz")
    relative_yaw = (yaw - body + math.pi) % (2 * math.pi) - math.pi
    if abs(relative_yaw) > math.radians(65):
        raise ValueError("Head yaw exceeds the SDK's relative body limit")
    if not np.allclose(head[3], [0, 0, 0, 1]) or abs(np.linalg.det(head[:3, :3]) - 1) > 0.02:
        raise ValueError("Invalid homogeneous head transform")
    head[:3, :3] = Rotation.from_euler(
        "xyz", [np.clip(roll, -0.7, 0.7), np.clip(pitch, -0.7, 0.7), yaw]
    ).as_matrix()
    head[:3, 3] = np.clip(head[:3, 3], [-0.04, -0.04, -0.05], [0.04, 0.04, 0.03])
    dt = min(max(dt, 0.001), 0.04)
    rotation = Rotation.from_matrix(previous.head[:3, :3].T @ head[:3, :3])
    angle = float(np.linalg.norm(rotation.as_rotvec()))
    distance = float(np.linalg.norm(head[:3, 3] - previous.head[:3, 3]))
    fraction = min(1.0, math.radians(80) * dt / max(angle, 1e-9), 0.06 * dt / max(distance, 1e-9))
    from reachy_mini.utils.interpolation import linear_pose_interpolation

    head = linear_pose_interpolation(previous.head, head, fraction)
    body = float(np.clip(body, previous.body - math.radians(60) * dt, previous.body + math.radians(60) * dt))
    roll, pitch, yaw = Rotation.from_matrix(head[:3, :3]).as_euler("xyz")
    relative_yaw = (yaw - body + math.pi) % (2 * math.pi) - math.pi
    yaw = body + float(np.clip(relative_yaw, -math.radians(65), math.radians(65)))
    head[:3, :3] = Rotation.from_euler("xyz", [roll, pitch, yaw]).as_matrix()
    antennas = np.clip(antennas, -math.radians(175), math.radians(175))
    antennas = np.clip(antennas, previous.antennas - math.pi * dt, previous.antennas + math.pi * dt)
    return Frame(head, antennas, body)
