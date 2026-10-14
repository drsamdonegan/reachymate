"""Labelled human speech survey; stores angles, never speech."""

from __future__ import annotations

import math
import time

from app.scripts.hw.common import parser, summary, write_report
from app.services.audio.xvf import XvfController


def bearing_error(samples, expected_degrees):
    angles = [math.degrees(math.pi / 2 - s[0]) for s in samples if s is not None and s[1]]
    errors = [abs(angle - expected_degrees) for angle in angles]
    correct = [angle * expected_degrees > 0 for angle in angles] if expected_degrees else []
    return {
        "speech_fraction": len(angles) / max(len(samples), 1),
        "error_degrees": summary(errors),
        "correct_side_fraction": sum(correct) / len(correct) if correct else None,
    }


def main():
    cli = parser("doa_survey")
    cli.add_argument("--bearing-degrees", type=float, required=True)
    cli.add_argument(
        "--label",
        required=True,
        choices=["left90", "left45", "front", "right45", "right90", "behind_left", "behind"],
    )
    cli.add_argument("--pose", required=True, choices=["asleep", "awake", "yaw10"])
    cli.add_argument("--seconds", type=float, default=10)
    args = cli.parse_args()
    if not 0 < args.seconds <= 60:
        raise ValueError("Survey duration must be within 60 seconds")
    xvf = XvfController()
    samples = []
    try:
        xvf.open()
        start = time.monotonic()
        while time.monotonic() - start < args.seconds:
            samples.append(xvf.read_doa())
            time.sleep(0.1)
        write_report(
            args.output,
            {
                "label": args.label,
                "pose": args.pose,
                "samples": samples,
                **bearing_error(samples, args.bearing_degrees),
                "status": "measured",
                "concurrent_audio": False,
                "validation_pending": "Repeat ten utterances per pose and with concurrent audio",
            },
        )
    finally:
        xvf.close()


if __name__ == "__main__":
    main()
