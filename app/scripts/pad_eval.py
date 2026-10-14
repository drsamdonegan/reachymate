"""Summarize consented, held-out PAD scores; this never grants release approval."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from app.services.vision.pad import ATTACK_SPECIES


def evaluate_scores(records: list[dict], *, passive_threshold=0.9, parallax_threshold=0.04):
    groups = {species: {"attempts": 0, "accepted": 0} for species in ATTACK_SPECIES}
    bona_fide, bona_fide_rejected = 0, 0
    for row in records:
        if not row.get("consent_id") or not row.get("participant_id"):
            raise ValueError("PAD evaluation needs separately consented pseudonymous participants")
        passive, residual = float(row["passive_score"]), float(row["parallax_residual"])
        if not math.isfinite(passive) or not math.isfinite(residual) or not 0 <= passive <= 1 or residual < 0:
            raise ValueError("Invalid PAD measurement")
        if row.get("frame_count", 0) < 5 or row.get("measured_yaw_span", 0) < 9:
            raise ValueError("Each PAD attempt needs five frames and a measured yaw sweep")
        accepted = residual >= parallax_threshold and (
            passive >= passive_threshold
            or (0.6 <= passive < passive_threshold and row.get("challenge_passed") is True)
        )
        species = row["species"]
        if species == "bona_fide":
            bona_fide += 1
            bona_fide_rejected += not accepted
        elif species in groups:
            groups[species]["attempts"] += 1
            groups[species]["accepted"] += accepted
        else:
            raise ValueError("Unknown attack species")
    for group in groups.values():
        count, accepted = group["attempts"], group["accepted"]
        group["apcer"] = accepted / count if count else None
        group["zero_error_upper_95"] = min(1.0, 3 / count) if count and not accepted else None
    return {
        "approved": False,
        "attacks": groups,
        "bona_fide_attempts": bona_fide,
        "bona_fide_rejected": bona_fide_rejected,
        "bpcer": bona_fide_rejected / bona_fide if bona_fide else None,
        "passive_threshold": passive_threshold,
        "parallax_threshold": parallax_threshold,
        "pilot_counts_sufficient": all(group["attempts"] >= 100 for group in groups.values()),
        "real_door_counts_sufficient": all(group["attempts"] >= 300 for group in groups.values()),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    corpus = json.loads(args.scores.read_text())
    if corpus.get("held_out") is not True:
        raise SystemExit("Use the frozen held-out participant split for PAD release evidence")
    report = evaluate_scores(corpus["records"])
    report.update(
        {
            key: corpus.get(key)
            for key in ("held_out", "model_sha256", "camera_unique_id", "calibration_sha256")
        }
    )
    args.output.write_text(json.dumps(report, indent=2))
    print({"output": str(args.output), "approved": False})


if __name__ == "__main__":
    main()
