"""Probe CLI and sanitised evidence writers."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import sys
import time
from pathlib import Path


def parser(name: str):
    value = argparse.ArgumentParser(description=name)
    value.add_argument("--output", type=Path, default=Path("docs/evidence/phase0") / f"{name}.json")
    return value


def write_report(path: Path, data: dict) -> dict:
    if shutil.disk_usage(path.parent if path.parent.exists() else Path.cwd()).free < 5 * 1024**3:
        raise RuntimeError("Phase 0 build/evidence disk floor is 5 GiB")
    report = {"measured_at_unix": time.time(), "python": sys.version.split()[0], "pid": os.getpid(), **data}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"output": str(path.resolve()), "status": report.get("status", "measured")}))
    return report


def versions():
    return {
        name: importlib.metadata.version(name)
        for name in ("reachy-mini", "opencv-python", "sherpa-onnx", "sherpa-onnx-core")
    }


def summary(values) -> dict:
    import numpy as np

    array = np.asarray(values, dtype=float)
    return {
        "count": len(array),
        "p50": float(np.median(array)) if len(array) else None,
        "p95": float(np.percentile(array, 95)) if len(array) else None,
        "max": float(np.max(array)) if len(array) else None,
    }
