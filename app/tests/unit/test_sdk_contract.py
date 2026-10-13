"""Check the installed dependency API without connecting to the robot."""

from __future__ import annotations

import inspect

import pytest


def test_sdk_contract_signatures():
    sdk = pytest.importorskip("reachy_mini")
    from reachy_mini.io.protocol import StopMoveCmd
    from reachy_mini.media.audio_doa import AudioDoA, ReSpeaker
    from reachy_mini.media.camera_utils import undistort_points
    from reachy_mini.utils.rotation import Rotation

    for name in (
        "goto_sleep",
        "wake_up",
        "enable_motors",
        "disable_motors",
        "goto_target",
        "set_target",
        "look_at_world",
    ):
        assert callable(getattr(sdk.ReachyMini, name))
    assert "ids" in inspect.signature(sdk.ReachyMini.enable_motors).parameters
    assert {"head", "antennas", "duration", "method", "body_yaw"} <= set(
        inspect.signature(sdk.ReachyMini.goto_target).parameters
    )
    assert "perform_movement" in inspect.signature(sdk.ReachyMini.look_at_world).parameters
    assert callable(AudioDoA.get_DoA)
    assert "verify" in inspect.signature(ReSpeaker.apply_audio_config).parameters
    assert StopMoveCmd().type == "stop_move"
    assert callable(undistort_points)
    assert callable(Rotation.from_matrix) and callable(Rotation.from_euler)


def test_keyword_tokens_exist():
    from pathlib import Path

    tokens_path = Path("data/models/sherpa-kws/tokens.txt")
    if not tokens_path.is_file():
        pytest.skip("Local model provisioning not installed")
    tokens = {line.split()[0] for line in tokens_path.read_text().splitlines()}
    for line in Path("app/assets/hey_reachy.keywords").read_text().splitlines():
        for token in line.split():
            if not token.startswith((":", "#", "@")):
                assert token in tokens


def test_realtime_sdk_stop_transport_contract():
    pytest.importorskip("reachy_mini")
    from reachy_mini.io.ws_client import WSClient

    assert "cmd" in inspect.signature(WSClient.send_command).parameters


def test_import_smoke_blocks_scipy_and_uses_opencv4(monkeypatch):
    import os
    import subprocess
    import sys

    code = """
import importlib, pkgutil, sys
import faulthandler
faulthandler.enable()
print('import:cv2,sherpa_onnx', file=sys.stderr, flush=True)
import cv2, sherpa_onnx
assert cv2.__version__.startswith('4.')
sys.modules['scipy'] = None
import app
errors = []
for module in pkgutil.walk_packages(app.__path__, prefix='app.'):
    if '.tests' in module.name:
        continue
    try:
        print('import:' + module.name, file=sys.stderr, flush=True)
        importlib.import_module(module.name)
    except Exception as exc:
        errors.append((module.name, type(exc).__name__))
assert not errors, errors
"""
    environment = {
        **os.environ,
        "APP_ENV": "test",
        "REACHY_ENABLED": "false",
        "REACHY_SIMULATION": "true",
        "REALTIME_ENABLED": "false",
        "DB_URL": "sqlite:///:memory:",
    }
    # The SDK loads GLib/GStreamer native frameworks into the test process.
    # Forking that warmed process on macOS can crash before exec (the failure
    # stack is subprocess._fork_exec, before the import smoke code runs).
    # Absolute executable + close_fds=False selects CPython's posix_spawn path;
    # Python-created descriptors retain their default close-on-exec flag.
    spawn_calls = []
    original_spawn = getattr(subprocess.Popen, "_posix_spawn", None)
    if original_spawn is not None:

        def recorded_spawn(self, *args, **kwargs):
            spawn_calls.append(True)
            return original_spawn(self, *args, **kwargs)

        monkeypatch.setattr(subprocess.Popen, "_posix_spawn", recorded_spawn)
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", code],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        close_fds=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if sys.platform == "darwin":
        assert spawn_calls, "macOS dependency smoke must use posix_spawn, never fork warmed SDK libraries"
