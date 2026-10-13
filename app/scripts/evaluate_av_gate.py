"""Replay consented feature logs through Gate D/P; never load images or audio."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

from app.services.audio.doa import DoaSample
from app.services.vision.attention import AttentionObservation
from app.services.vision.av_gate import AVGate


def evaluate_features(rows: list[dict], *, mode: str = "pilot", doa_validated: bool = True) -> dict:
    """Rows carry human-labelled talker/conditions, with no identities."""
    groups = defaultdict(list)
    for row in rows:
        if row.get("label") not in {"visitor", "bystander", "rear", "silence"}:
            raise ValueError("Feature logs require visitor/bystander/rear/silence labels")
        groups[(str(row["session"]), float(row["noise_dba"]))].append(row)
    statistics = defaultdict(
        lambda: {
            "sessions": 0,
            "visitor": 0,
            "clipped": 0,
            "bystander": 0,
            "false_open": 0,
            "rear": 0,
            "rear_open": 0,
            "barge_ins": 0,
        }
    )
    for (_, noise), frames in groups.items():
        gate = AVGate(mode=mode, doa_validated=doa_validated)
        stats = statistics[noise]
        stats["sessions"] += 1
        for row in sorted(frames, key=lambda frame: float(frame["time_s"])):
            now = float(row["time_s"])
            face_row = row.get("face")
            face = (
                AttentionObservation(
                    int(face_row["track"]),
                    (0, 0, 150, 150),
                    math.radians(float(face_row["bearing_deg"])),
                    now - float(face_row.get("age_s", 0)),
                    bool(face_row.get("in_zone", True)),
                    face_row.get("mouth_active"),
                    float(face_row.get("mouth_still_s", 0)),
                )
                if face_row
                else None
            )
            doa_row = row.get("doa")
            doa = (
                DoaSample(
                    float(doa_row["theta"]),
                    bool(doa_row.get("speech", False)),
                    now - float(doa_row.get("age_s", 0)),
                )
                if doa_row
                else None
            )
            result = gate.process(bytes(2560), vad_score=float(row["vad"]), face=face, doa=doa, now=now)
            label = row["label"]
            if label == "visitor":
                stats["visitor"] += 1
                stats["clipped"] += not result.opened
            elif label == "bystander":
                stats["bystander"] += 1
                stats["false_open"] += result.opened
            elif label == "rear":
                stats["rear"] += 1
                stats["rear_open"] += result.opened
            stats["barge_ins"] += bool(row.get("bystander_barge_in", False))
    results = {}
    for noise, stats in statistics.items():
        false_open = stats["false_open"] / stats["bystander"] if stats["bystander"] else None
        clipped = stats["clipped"] / stats["visitor"] if stats["visitor"] else None
        rear = stats["rear_open"] / stats["rear"] if stats["rear"] else None
        results[str(noise)] = {
            **stats,
            "bystander_false_open_fraction": false_open,
            "visitor_clipped_fraction": clipped,
            "rear_turn_fraction": rear,
            "passed": stats["sessions"] >= 20
            and false_open is not None
            and false_open <= (0.05 if mode == "pilot" else 0.10)
            and clipped is not None
            and clipped <= (0.03 if mode == "pilot" else 0.05)
            and (
                mode != "pilot"
                or rear is not None
                and rear <= 0.02
                and stats["barge_ins"] <= stats["sessions"] / 20
            ),
        }
    required_noise = {"55.0", "65.0", "72.0"}
    return {
        "mode": mode,
        "noise_levels": results,
        "passed": required_noise <= set(results) and all(v["passed"] for v in results.values()),
        "limitation": "Feature replay must be checked against attended doorway runs; "
        "no hardware measurement is fabricated",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("features", type=Path)
    parser.add_argument("--mode", choices=["demo", "pilot"], default="pilot")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate_features(json.loads(args.features.read_text())["frames"], mode=args.mode)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(args.output.resolve()), "passed": report["passed"]}))


if __name__ == "__main__":
    main()
