"""Physical camera mapping, warmup/FPS and optional checkerboard K/D in RAM."""

from __future__ import annotations

import time

import numpy as np

from app.scripts.hw.common import parser, write_report
from app.services.system.tcc import camera_devices, permission_status, resolve_camera


def main():
    cli = parser("camera_probe")
    cli.add_argument("--unique-id")
    cli.add_argument("--seconds", type=float, default=10)
    cli.add_argument("--checkerboard", help="Inner corner columns x rows, e.g. 9x6")
    args = cli.parse_args()
    if not 0 < args.seconds <= 60:
        raise ValueError("Probe duration must be within 60 seconds")
    tcc = permission_status()
    devices = camera_devices()
    if tcc.get("camera") in {"denied", "restricted"}:
        write_report(args.output, {"status": "blocked", "tcc": tcc, "devices": devices})
        return
    import cv2

    index = resolve_camera(unique_id=args.unique_id)
    capture = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    if not capture.isOpened():
        raise RuntimeError("Physical Reachy camera failed to open")
    corners = []
    frames = 0
    started = time.monotonic()
    shape = None
    try:
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        for _ in range(15):
            capture.read()
        warmed = time.monotonic()
        board = tuple(map(int, args.checkerboard.lower().split("x"))) if args.checkerboard else None
        last_corner = 0.0
        while time.monotonic() - warmed < args.seconds:
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError("Camera stopped delivering frames")
            shape = frame.shape
            frames += 1
            if board and time.monotonic() - last_corner > 0.4:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                found, points = cv2.findChessboardCorners(gray, board)
                if found:
                    points = cv2.cornerSubPix(gray, points, (11, 11), (-1, -1), (3, 30, 0.001))
                    corners.append(points)
                    last_corner = time.monotonic()
        result = {
            "tcc": tcc,
            "devices": devices,
            "index": index,
            "frames": frames,
            "warmup_seconds": warmed - started,
            "fps": frames / (time.monotonic() - warmed),
            "width": shape[1],
            "height": shape[0],
            "status": "measured",
            "validated": False,
            "validation_pending": "Tape test at 0, +/-15, +/-30, +/-35 degrees",
        }
        if board and len(corners) >= 10:
            object_points = np.zeros((board[0] * board[1], 3), np.float32)
            object_points[:, :2] = np.mgrid[0 : board[0], 0 : board[1]].T.reshape(-1, 2)
            error, matrix, distortion, _, _ = cv2.calibrateCamera(
                [object_points] * len(corners), corners, (shape[1], shape[0]), None, None
            )
            result.update(
                {
                    "K": matrix.tolist(),
                    "D": distortion.flatten().tolist(),
                    "reprojection_error_px": error,
                    "checkerboard_frames": len(corners),
                    "W_zone": float(matrix[0, 0] * 0.15 / 1.5),
                    "W_crop": float(matrix[0, 0] * 0.15 / 1.1),
                }
            )
        write_report(args.output, result)
    finally:
        capture.release()


if __name__ == "__main__":
    main()
