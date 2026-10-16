from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from app.models.enums import AppState
from app.runtime import AppContext
from app.services.reachy.motion.commands import RobotMotionRequest
from app.services.reachy.motion.expressions import LIBRARIES, Frame, MotionLibrary, MotionSequence, safe_frame
from app.services.reachy.motion.manager import MotionManager, MotionMode
from app.services.realtime.tools import ToolRegistry


def test_finite_ik_solution_must_also_respect_physical_motor_limits():
    from app.services.reachy.motion.expressions import reachable_joints

    assert reachable_joints(np.radians([0, 30, -30, 30, -30, 30, -30]))
    assert not reachable_joints(np.radians([0, 95, -30, 30, -30, 30, -30]))
    assert not reachable_joints(np.radians([170, 30, -30, 30, -30, 30, -30]))


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "unlock"},
        {"action": "pose", "yaw_degrees": float("nan")},
        {"action": "pose", "body_yaw_degrees": 161},
        {"action": "pose", "left_antenna_degrees": 180},
        {"action": "dance", "name": "../../secret"},
        {"action": "dance", "repeat": 100},
        {"action": "stop", "torque": "off"},
        {"action": "pose", "x_mm": True},
    ],
)
def test_motion_rejects_invalid_axes_and_sdk_or_door_commands(arguments):
    with pytest.raises(ValidationError):
        RobotMotionRequest.model_validate(arguments)


@pytest.mark.asyncio
async def test_stop_remains_available_after_tool_budget_is_exhausted():
    calls = []
    registry = ToolRegistry(
        handlers={"robot_motion": lambda args, call: calls.append(args) or {"status": "stopped"}},
        session_limit=1,
    )
    await registry.execute("wait_for_user", {}, call_id="used")
    assert (await registry.execute("robot_motion", '{"action":"stop"}', call_id="stop"))[
        "status"
    ] == "stopped"
    assert len(calls) == 1
    assert (await registry.execute("robot_motion", {"action": "dance"}, call_id="later"))[
        "status"
    ] == "rate_limited"


@pytest.mark.asyncio
async def test_runtime_blocks_motion_during_consent_and_rest_but_always_allows_stop(settings):
    calls = []

    async def perform(request):
        calls.append(request.action)
        return {"status": "started" if request.action != "stop" else "stopped"}

    ctx = AppContext(settings=settings)
    ctx.robot = SimpleNamespace(perform_motion=perform)
    assert (await ctx._robot_motion({"action": "dance"}))["status"] == "blocked"
    assert (await ctx._robot_motion({"action": "stop"}))["status"] == "stopped"
    ctx.guard.begin_engagement(30)
    ctx.state_machine.state = AppState.ENGAGED
    ctx.realtime = SimpleNamespace(_input_suspended=True)
    assert (await ctx._robot_motion({"action": "pose", "yaw_degrees": 10}))["status"] == "blocked"
    assert calls == ["stop"]
    await ctx.booking_client.close()
    await ctx.public_knowledge.close()
    await ctx.frontdesk_client.close()


def test_library_requires_pinned_revision_and_matching_file_digest(tmp_path):
    path = tmp_path / "dances"
    path.mkdir()
    data = {
        "description": "test",
        "time": [0.0, 0.2, 0.4],
        "set_target_data": [
            {"head": np.eye(4).tolist(), "antennas": [0.0, 0.0], "body_yaw": 0.0} for _ in range(3)
        ],
    }
    move = path / "sample.json"
    move.write_text(json.dumps(data))
    repo, revision = LIBRARIES["dances"]
    manifest = {
        "dances": {
            "repo": repo,
            "revision": revision,
            "files": {move.name: hashlib.sha256(move.read_bytes()).hexdigest()},
        }
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert MotionLibrary(tmp_path).capabilities()["dances"] == ["sample"]
    move.write_text(move.read_text() + " ")
    assert not MotionLibrary(tmp_path).capabilities()["dances"]


def test_full_body_and_head_pose_duration_respects_slew_and_relative_yaw(tmp_path):
    pytest.importorskip("reachy_mini")
    from reachy_mini.utils import create_head_pose

    request = RobotMotionRequest(
        action="pose",
        body_yaw_degrees=140,
        duration_seconds=0.5,
        right_antenna_degrees=120,
        left_antenna_degrees=-120,
    )
    sequence = MotionLibrary(tmp_path).sequence(request, Frame.neutral())
    assert 4 < sequence.duration < 20
    final = sequence.evaluate(sequence.duration)
    assert final.body == pytest.approx(np.radians(140))
    assert final.antennas == pytest.approx(np.radians([120, -120]))
    previous = Frame.neutral()
    for elapsed in np.arange(0, sequence.duration + 0.02, 0.02):
        current = safe_frame(sequence.evaluate(float(elapsed)), previous, 0.02)
        assert abs(current.body - previous.body) <= np.radians(60) * 0.02 + 1e-9
        previous = current
    assert previous.body == pytest.approx(final.body, abs=0.01)
    reduced = sequence.scaled(0.5)
    assert reduced.amplitude_scale == 0.5
    assert reduced.evaluate(reduced.duration).body == pytest.approx(np.radians(70))
    invalid = Frame(create_head_pose(yaw=90), np.zeros(2), 0.0)
    with pytest.raises(ValueError, match="relative body"):
        safe_frame(invalid, Frame.neutral(), 0.02)


@pytest.mark.asyncio
async def test_one_motion_owner_handles_all_actuators_and_freeze_cancels_playback():
    pytest.importorskip("reachy_mini")
    from reachy_mini.utils import create_head_pose

    writes = []
    motion = MotionManager(
        SimpleNamespace(set_target=lambda **kw: writes.append((threading.get_ident(), kw)))
    )
    motion.start()
    motion.set_mode(MotionMode.ENGAGED)
    end = Frame(create_head_pose(yaw=10), np.radians([20.0, -20.0]), np.radians(5))
    sequence = MotionSequence([(0.0, Frame.neutral()), (0.5, end)], label="test")
    assert motion.play_expression(sequence)
    await asyncio.sleep(0.12)
    assert motion.expression_diagnostics()["active"]
    pending_ticket = motion.reserve_expression()
    motion.stop_expression()
    assert not motion.play_expression(sequence, generation=pending_ticket)
    assert not motion.expression_diagnostics()["active"]
    assert motion.play_expression(sequence)
    motion.set_mode(MotionMode.CAPTURE_FREEZE)
    await asyncio.sleep(0.04)
    count = len(writes)
    await asyncio.sleep(0.04)
    assert len(writes) == count
    assert not motion.play_expression(sequence)
    await motion.stop()
    assert len({tid for tid, _ in writes}) == 1
    assert all({"head", "antennas", "body_yaw"} <= frame.keys() for _, frame in writes)
