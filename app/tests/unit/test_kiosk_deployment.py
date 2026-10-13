import importlib.util
import json
import plistlib
from pathlib import Path


def load(name):
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(name, root / "deploy/macos" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agents_have_safe_flags_and_keep_tcc_parent():
    values = load("generate_agents").agents(Path("/tmp/Reachy Robot"), Path("/tmp/logs"))
    daemon = values["daemon"]["ProgramArguments"]
    assert "--goto-sleep-on-stop" in daemon and "--no-wake-up-on-start" in daemon
    assert "--fastapi-host" in daemon and "127.0.0.1" in daemon
    bouncer = values["bouncer"]["ProgramArguments"]
    assert bouncer[0].endswith("Reachy Concierge.app/Contents/MacOS/ReachyConcierge")
    assert "caffeinate" not in bouncer[0]
    for value in values.values():
        assert plistlib.loads(plistlib.dumps(value)) == value
        assert value["ExitTimeOut"] == 30 and value["LimitLoadToSessionType"] == "Aqua"
        assert value["EnvironmentVariables"]["PHYSICAL_UNLOCK_ENABLED"] == "false"


def test_watchdog_measures_heartbeat(tmp_path):
    path = tmp_path / "beat.json"
    watchdog = load("watchdog")
    assert watchdog.stale(path, now=100)
    path.write_text(json.dumps({"timestamp": 90}))
    assert not watchdog.stale(path, now=100)
    assert watchdog.stale(path, now=151)
    assert watchdog.stale(path, now=80)
