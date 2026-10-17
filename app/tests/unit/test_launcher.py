from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.scripts import launch_reachy as launcher


def test_dashboard_auto_chat_requires_attended_staging_profile():
    assert not Settings(_env_file=None).dashboard_auto_chat
    with pytest.raises(ValidationError, match="auto chat requires attended"):
        Settings(_env_file=None, dashboard_auto_chat=True)
    profile = dict(
        _env_file=None,
        app_env="staging",
        voice_api="gpt_live",
        allow_dev_endpoints=True,
        access_mode="shadow",
        attended_chat_enabled=True,
        dashboard_auto_chat=True,
    )
    assert Settings(**profile).dashboard_auto_chat
    for override in ({"app_env": "production"}, {"physical_unlock_enabled": True}, {"voice_api": "realtime"}):
        with pytest.raises(ValidationError):
            Settings(**(profile | override))


def test_ready_stack_is_reused_without_creating_duplicate_media_owners(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "probe_service", Mock(return_value=True))
    spawn = Mock()
    monkeypatch.setattr(launcher, "spawn_service", spawn)
    for service in launcher.SERVICES:
        launcher.ensure_service(service, tmp_path, tmp_path)
    spawn.assert_not_called()


def test_service_waits_for_readiness_after_starting_only_once(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "probe_service", Mock(side_effect=[False, False, True]))
    monkeypatch.setattr(launcher, "port_open", lambda _: False)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    process = SimpleNamespace(poll=lambda: None)
    spawn = Mock(return_value=process)
    monkeypatch.setattr(launcher, "spawn_service", spawn)
    launcher.ensure_service(launcher.SERVICES[0], tmp_path, tmp_path)
    spawn.assert_called_once()


def test_occupied_unready_port_never_starts_another_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "probe_service", lambda _: False)
    monkeypatch.setattr(launcher, "port_open", lambda _: True)
    spawn = Mock()
    monkeypatch.setattr(launcher, "spawn_service", spawn)
    with pytest.raises(launcher.LaunchError, match="USB and external power"):
        launcher.ensure_service(launcher.SERVICES[0], tmp_path, tmp_path, timeout=0)
    spawn.assert_not_called()


def test_crashed_child_reports_log_without_an_unbounded_restart_loop(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "probe_service", lambda _: False)
    monkeypatch.setattr(launcher, "port_open", lambda _: False)
    spawn = Mock(return_value=SimpleNamespace(poll=lambda: 1))
    monkeypatch.setattr(launcher, "spawn_service", spawn)
    with pytest.raises(launcher.LaunchError, match="launcher-app.log"):
        launcher.ensure_service(launcher.SERVICES[-1], tmp_path, tmp_path)
    spawn.assert_called_once()


def test_second_launcher_cannot_acquire_the_hardware_start_lock(tmp_path):
    with launcher.launch_lock(tmp_path):
        with pytest.raises(launcher.LaunchError, match="Another Reachy launcher"):
            with launcher.launch_lock(tmp_path, timeout=0):
                pytest.fail("A second launcher acquired the startup lock")
    with launcher.launch_lock(tmp_path, timeout=0):
        pass


def test_reuse_rejects_wrong_service_and_simulated_or_media_owning_daemon(monkeypatch):
    payload = dict(type="daemon_status", state="running", no_media=True, backend_status={"ready": True})
    monkeypatch.setattr(launcher, "request_json", lambda _: payload)
    daemon = launcher.SERVICES[0]
    assert launcher.probe_service(daemon)
    for override in ({"no_media": False}, {"simulation_enabled": True}, {"type": "another_service"}):
        current = payload | override
        monkeypatch.setattr(launcher, "request_json", lambda _, value=current: value)
        with pytest.raises(launcher.LaunchError):
            launcher.probe_service(daemon)
    monkeypatch.setattr(launcher, "request_json", lambda _: payload | {"backend_status": {"ready": False}})
    assert not launcher.probe_service(daemon)


def test_spawn_preserves_native_tcc_launch_and_forces_the_local_gpt_live_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setenv("VOICE_API", "realtime")
    popen = Mock(return_value=SimpleNamespace(pid=123))
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    root = tmp_path / "Reachy Robot"
    launcher.spawn_service(launcher.SERVICES[-1], root, tmp_path)
    args, kwargs = popen.call_args
    assert args[0][0] == str(root / ".venv/bin/python")
    assert args[0][-2:] == ["--demo", "--auto-chat"]
    assert kwargs["close_fds"] is False and kwargs["start_new_session"] is True
    assert kwargs["env"]["VOICE_API"] == "gpt_live"
    assert kwargs["env"]["HOST"] == "127.0.0.1"
    assert (tmp_path / "launcher-app.log").stat().st_mode & 0o777 == 0o600


def test_old_app_is_not_silently_reused_for_another_api(monkeypatch):
    request = Mock(return_value={"voice": {"api": "realtime"}, "conversation": {}})
    monkeypatch.setattr(launcher, "request_json", request)
    with pytest.raises(launcher.LaunchError, match="older or different app"):
        launcher.connect_chat()
    assert request.call_count == 1


def test_launcher_connects_chat_and_leaves_diagnostics_open_on_cloud_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "ensure_service", Mock())
    connect = Mock(side_effect=launcher.LaunchError("GPT-Live is unavailable"))
    monkeypatch.setattr(launcher, "connect_chat", connect)
    browser = Mock()
    monkeypatch.setattr(launcher.webbrowser, "open", browser)
    with pytest.raises(launcher.LaunchError, match="GPT-Live is unavailable"):
        launcher.launch(tmp_path)
    browser.assert_called_once_with("http://127.0.0.1:8080/")
