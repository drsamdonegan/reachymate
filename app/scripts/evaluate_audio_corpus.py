"""Evaluate a consented, labelled doorway corpus against release gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import Settings
from app.services.audio.evaluation import evaluate_corpus, load_manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--split", default="validation")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--no-gate", action="store_true")
    args = parser.parse_args()

    evaluation = evaluate_corpus(Settings(), load_manifest(args.manifest, split=args.split))
    rendered = json.dumps(evaluation.as_dict(), indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        args.report.write_text(rendered + "\n", encoding="utf-8")
    if not args.no_gate and not evaluation.passed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
