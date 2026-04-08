import argparse
from unittest.mock import MagicMock

import pytest

import reachy_mini_conversation_app.main as main_mod


def _args(**overrides: object) -> argparse.Namespace:
    defaults = {
        "head_tracker": None,
        "no_camera": False,
        "local_vision": False,
        "gradio": False,
        "debug": False,
        "wireless_version": False,
        "on_device": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_build_connection_help_for_dashboard() -> None:
    """Dashboard help text should point to the daemon and dashboard launchers."""
    message = main_mod._build_connection_help(_args(), custom_dashboard_mode=True)

    assert "reachy-mini-daemon --no-localhost-only --log-level INFO" in message
    assert "reachy-convo-dashboard" in message
    assert "python -m reachy_mini_conversation_app.main" in message
    assert "http://127.0.0.1:8000/" in message


def test_standalone_main_passes_args_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Standalone entrypoint should pass parsed args through unchanged."""
    args = _args(gradio=True)
    monkeypatch.setattr(main_mod, "parse_args", lambda: (args, []))
    run_mock = MagicMock()
    monkeypatch.setattr(main_mod, "run", run_mock)

    main_mod.standalone_main()

    run_mock.assert_called_once_with(args)


def test_dashboard_main_runs_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard entrypoint should run the ReachyMiniApp wrapper."""
    app = MagicMock()
    monkeypatch.setattr(main_mod, "parse_args", lambda: (_args(), []))
    monkeypatch.setattr(main_mod, "ReachyConvoMate", lambda: app)

    main_mod.dashboard_main()

    app.wrapped_run.assert_called_once_with()
    app.stop.assert_not_called()


def test_dashboard_main_stops_on_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard entrypoint should stop cleanly on Ctrl-C."""
    app = MagicMock()
    app.wrapped_run.side_effect = KeyboardInterrupt
    monkeypatch.setattr(main_mod, "parse_args", lambda: (_args(), []))
    monkeypatch.setattr(main_mod, "ReachyConvoMate", lambda: app)

    main_mod.dashboard_main()

    app.stop.assert_called_once_with()


def test_dashboard_main_rewrites_connection_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard entrypoint should raise daemon-first guidance on connection failure."""
    app = MagicMock()
    app.wrapped_run.side_effect = ConnectionError("boom")
    monkeypatch.setattr(main_mod, "parse_args", lambda: (_args(), []))
    monkeypatch.setattr(main_mod, "ReachyConvoMate", lambda: app)

    with pytest.raises(ConnectionError) as exc_info:
        main_mod.dashboard_main()

    message = str(exc_info.value)
    assert "reachy-mini-daemon --no-localhost-only --log-level INFO" in message
    assert "reachy-convo-dashboard" in message


def test_recommended_daemon_command_prefers_no_localhost_only_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """MacOS should prefer the daemon mode that exposes Zenoh on IPv4."""
    monkeypatch.setattr(main_mod.sys, "platform", "darwin")

    assert main_mod._recommended_daemon_command() == "reachy-mini-daemon --no-localhost-only --log-level INFO"


def test_dashboard_main_forces_network_mode_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Older macOS SDK versions should avoid localhost-only Zenoh."""
    app = MagicMock()
    app.daemon_on_localhost = True
    monkeypatch.setattr(main_mod.sys, "platform", "darwin")
    monkeypatch.setattr(main_mod, "version", lambda _: "1.5.9")
    monkeypatch.setattr(main_mod, "parse_args", lambda: (_args(), []))
    monkeypatch.setattr(main_mod, "ReachyConvoMate", lambda: app)

    main_mod.dashboard_main()

    assert app.daemon_on_localhost is False
    app.wrapped_run.assert_called_once_with()


def test_dashboard_main_keeps_localhost_mode_on_newer_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Newer macOS SDK versions should keep localhost mode for local daemon access."""
    app = MagicMock()
    app.daemon_on_localhost = True
    monkeypatch.setattr(main_mod.sys, "platform", "darwin")
    monkeypatch.setattr(main_mod, "version", lambda _: "1.6.0")
    monkeypatch.setattr(main_mod, "parse_args", lambda: (_args(), []))
    monkeypatch.setattr(main_mod, "ReachyConvoMate", lambda: app)

    main_mod.dashboard_main()

    assert app.daemon_on_localhost is True
    app.wrapped_run.assert_called_once_with()


def test_dashboard_main_uses_no_video_backend_for_no_camera(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard entrypoint should skip video initialization when requested."""
    app = MagicMock()
    monkeypatch.setattr(main_mod, "parse_args", lambda: (_args(no_camera=True), []))
    monkeypatch.setattr(main_mod, "ReachyConvoMate", lambda: app)

    main_mod.dashboard_main()

    assert app.media_backend == "default_no_video"


def test_dashboard_main_rewrites_camera_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard entrypoint should surface camera permission guidance."""
    app = MagicMock()
    app.wrapped_run.side_effect = RuntimeError("Camera not found")
    monkeypatch.setattr(main_mod, "parse_args", lambda: (_args(), []))
    monkeypatch.setattr(main_mod, "ReachyConvoMate", lambda: app)

    with pytest.raises(RuntimeError) as exc_info:
        main_mod.dashboard_main()

    message = str(exc_info.value)
    assert "Grant camera access" in message
    assert "reachy-convo-dashboard --no-camera" in message
