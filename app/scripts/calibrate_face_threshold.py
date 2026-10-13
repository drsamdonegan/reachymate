"""Leave-one-template/identity-out ROC evidence using the runtime identity scorer.

This local analysis is not an approval to ship a threshold. Repeated templates
from one person are correlated; public impostor and held-out doorway trials are
needed before claiming the plan's target FPIR.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from pathlib import Path

import numpy as np

from app.config import get_settings
from app.services.vision.embedding_guard import zeroize
from app.services.vision.face_match import FaceMatcher
from app.vision_stack import authorized_vision_settings, build_vision_stack


def calibrate_gallery(
    gallery: dict[str, list[list[float]]],
    *,
    threshold: float = 0.48,
    gallery_size: int = 150,
    target_fpir: float = 0.001,
) -> dict:
    matcher = FaceMatcher()
    genuine_scores, identity_impostor_scores, nonmember_scores = [], [], []
    genuine_pairs, impostor_pairs = [], []
    for user_id, vectors in gallery.items():
        for index, probe in enumerate(vectors):
            remaining = vectors[:index] + vectors[index + 1 :]
            if remaining:
                genuine_scores.append(matcher.score_identities(probe, {user_id: remaining})[user_id])
            others = {other: templates for other, templates in gallery.items() if other != user_id}
            scores = matcher.score_identities(probe, others)
            identity_impostor_scores.extend(scores.values())
            if scores:
                nonmember_scores.append(max(scores.values()))
            # Each distinct pair is reported as supporting diagnostics; the ROC
            # uses identity aggregation rather than the old maximum-template rule.
            for other_index in range(index + 1, len(vectors)):
                genuine_pairs.append(
                    matcher.score_identities(probe, {"pair": [vectors[other_index]]})["pair"]
                )
            for other in sorted(gallery):
                if other > user_id:
                    for other_probe in gallery[other]:
                        impostor_pairs.append(
                            matcher.score_identities(probe, {"pair": [other_probe]})["pair"]
                        )
    if not genuine_scores or not identity_impostor_scores:
        raise ValueError("Need at least two members and multiple templates per member")
    thresholds = sorted(set([-1.0, 1.000001, threshold] + genuine_scores + identity_impostor_scores))
    roc = []
    for candidate in thresholds:
        fmr = sum(score >= candidate for score in identity_impostor_scores) / len(identity_impostor_scores)
        tpr = sum(score >= candidate for score in genuine_scores) / len(genuine_scores)
        fpir = sum(score >= candidate for score in nonmember_scores) / len(nonmember_scores)
        roc.append(
            {
                "threshold": candidate,
                "tpr": tpr,
                "fnir": 1 - tpr,
                "identity_fmr": fmr,
                "leave_one_identity_out_fpir": fpir,
                "projected_fpir": min(1.0, gallery_size * fmr),
            }
        )
    chosen = next(row for row in roc if row["threshold"] == threshold)
    impostor_errors = sum(score >= threshold for score in identity_impostor_scores)
    request_errors = sum(score >= threshold for score in nonmember_scores)
    # Rule of three is only applicable when zero errors have been observed.
    comparison_bound = min(1.0, 3 / len(identity_impostor_scores)) if impostor_errors == 0 else None
    request_bound = min(1.0, 3 / len(nonmember_scores)) if request_errors == 0 else None
    eligible = [row for row in roc if row["projected_fpir"] <= target_fpir]
    candidate = max(eligible, key=lambda row: row["tpr"]) if eligible else None
    return {
        "model": "top_three_mean_cosine",
        "identity_count": len(gallery),
        "template_count": sum(map(len, gallery.values())),
        "genuine_template_pairs": len(genuine_pairs),
        "impostor_template_pairs": len(impostor_pairs),
        "genuine_probes": len(genuine_scores),
        "impostor_identity_comparisons": len(identity_impostor_scores),
        "nonmember_proxy_probes": len(nonmember_scores),
        "evaluation_gallery_size": gallery_size,
        "at_threshold": chosen,
        "impostor_errors": impostor_errors,
        "request_errors": request_errors,
        "zero_error_comparison_upper_95": comparison_bound,
        "zero_error_request_upper_95": request_bound,
        "zero_error_projected_fpir_upper_95": min(1.0, gallery_size * comparison_bound)
        if comparison_bound is not None
        else None,
        "target_fpir": target_fpir,
        "required_impostor_comparisons_if_zero_errors": int(np.ceil(3 * gallery_size / target_fpir)),
        "candidate_threshold": candidate["threshold"] if candidate else None,
        "shipping_ready": False,
        "roc": roc,
        "limitations": (
            "In-sample, correlated templates and leave-one-identity-out proxies; "
            "no held-out subgroup or PAD evidence."
        ),
    }


def write_roc(path: Path, report: dict) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(report["roc"][0]))
        writer.writeheader()
        writer.writerows(report["roc"])


async def load_encrypted_gallery(settings):
    async with authorized_vision_settings(settings) as scoped_settings:
        stack = build_vision_stack(scoped_settings)
        try:
            return stack.face_store.list_embeddings(model_id=stack.embedder.model_id)
        finally:
            stack.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Produce calibrated identity ROC data without running the robot"
    )
    parser.add_argument(
        "--fixture-gallery",
        type=Path,
        help="Pseudonymous evaluation fixture JSON, never real raw face images",
    )
    parser.add_argument("--threshold", type=float, default=0.48)
    parser.add_argument("--gallery-size", type=int, default=150)
    parser.add_argument("--target-fpir", type=float, default=0.001)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--roc-csv", type=Path)
    parser.add_argument(
        "--plot", type=Path, help="Optional ROC plot; requires matplotlib in the evaluation environment"
    )
    args = parser.parse_args()
    if args.gallery_size < 1 or not 0 < args.target_fpir < 1:
        parser.error("gallery size and target FPIR must be positive")
    stack = None
    gallery = {}
    try:
        if args.fixture_gallery:
            gallery = json.loads(args.fixture_gallery.read_text())
        else:
            gallery = asyncio.run(load_encrypted_gallery(get_settings()))
        report = calibrate_gallery(
            gallery, threshold=args.threshold, gallery_size=args.gallery_size, target_fpir=args.target_fpir
        )
        args.output.write_text(json.dumps(report, indent=2))
        if args.roc_csv:
            write_roc(args.roc_csv, report)
        if args.plot:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            fig, ax = plt.subplots()
            ax.plot([row["identity_fmr"] for row in report["roc"]], [row["tpr"] for row in report["roc"]])
            ax.set(
                xlabel="False match rate per identity comparison",
                ylabel="True positive rate",
                title="Local identity ROC — held-out validation still required",
                xlim=(0, 1),
                ylim=(0, 1),
            )
            fig.savefig(args.plot, bbox_inches="tight")
            plt.close(fig)
        print(
            {
                "output": str(args.output),
                "shipping_ready": False,
                "genuine_probes": report["genuine_probes"],
                "impostor_identity_comparisons": report["impostor_identity_comparisons"],
            }
        )
    finally:
        for vectors in gallery.values():
            zeroize(vectors)
        if stack:
            stack.close()


if __name__ == "__main__":
    main()
