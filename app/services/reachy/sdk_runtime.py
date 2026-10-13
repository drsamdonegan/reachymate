"""Expose the SDK wheel's bundled GI runtime without shell-specific paths."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys


def configure_sdk_runtime() -> None:
    if sys.platform != "darwin" or importlib.util.find_spec("gstreamer_python") is None:
        return
    # The wheel exports its own relocatable environment; no Homebrew paths.
    environment = importlib.import_module("gstreamer_python").environment
    for path in environment.get("PYTHONPATH", "").split(os.pathsep):
        if path and path not in sys.path:
            sys.path.append(path)
    for name, value in environment.items():
        existing = os.environ.get(name, "")
        if value not in existing.split(os.pathsep):
            os.environ[name] = value + (os.pathsep + existing if existing else "")
    os.environ.setdefault("GST_REGISTRY_FORK", "no")
