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


def test_community_timeline_accepts_sdk_recordings_with_nonzero_start_and_duplicate_samples():
    frame = {"head": np.eye(4).tolist(), "antennas": [0.0, 0.0]}
    MotionLibrary._validate_recording({"time": [0.035, 0.035, 90.0], "set_target_data": [frame] * 3})
    for times in ([0.0, 0.0, 0.0], [0.0, 2.0, 1.0], [0.0, 1.0, 601.0]):
        with pytest.raises(ValueError, match="timeline"):
            MotionLibrary._validate_recording({"time": times, "set_target_data": [frame] * 3})


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
    sequence = MotionSequence([(0.0, Frame.neutral()), (5.0, end)], label="test")
    assert motion.play_expression(sequence)
    async with asyncio.timeout(3):
        while len(writes) < 3:
            await asyncio.sleep(0.01)
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


@pytest.mark.asyncio
async def test_speech_moves_a_held_pose_after_expression_but_stop_and_freeze_hold_still():
    pytest.importorskip("reachy_mini")
    from reachy_mini.utils import create_head_pose

    writes = []
    motion = MotionManager(SimpleNamespace(set_target=lambda **kw: writes.append(kw)))
    motion.start()
    try:
        motion.set_mode(MotionMode.ENGAGED)
        end = Frame(create_head_pose(yaw=10), np.radians([-10, 10]), 0.0)
        sequence = MotionSequence([(0, Frame.neutral()), (0.1, end)], label="look")
        assert motion.play_expression(sequence)
        async with asyncio.timeout(3):
            while motion.expression_diagnostics()["status"] != "completed":
                await asyncio.sleep(0.01)
        before = motion.frame().head.copy()
        motion.set_speech_offsets((0.0, 0.0, 0.002, 0.03, 0.06, 0.04))
        await asyncio.sleep(0.14)
        assert not np.allclose(motion.frame().head, before, atol=0.001)
        assert motion.expression_diagnostics()["speech_active"]
        motion.stop_expression()
        stopped = motion.frame().head.copy()
        motion.set_speech_offsets((0.004, 0.004, 0.0, -0.05, -0.05, -0.04))
        await asyncio.sleep(0.12)
        assert np.allclose(motion.frame().head, stopped, atol=1e-6)
        assert not motion.expression_diagnostics()["speech_motion_enabled"]
        # A new requested motion restores talking movement after completion.
        generation = motion.reserve_expression()
        assert motion.play_expression(sequence, generation=generation)
        assert motion.expression_diagnostics()["speech_motion_enabled"]
        motion.set_mode(MotionMode.CAPTURE_FREEZE)
        await asyncio.sleep(0.05)
        count = len(writes)
        motion.set_speech_offsets((0.004, 0.0, 0.0, 0.1, 0.1, 0.1))
        await asyncio.sleep(0.05)
        assert len(writes) == count
        assert not motion.error
    finally:
        await motion.stop()


def community_fixture(tmp_path, count=1):
    from app.services.reachy.motion.community import recording_id

    directory = tmp_path / "community"
    directory.mkdir()
    data = {
        "time": [0.0, 0.5, 1.0],
        "description": "A joyful dance",
        "set_target_data": [{"head": np.eye(4).tolist(), "antennas": [0.0, 0.0], "body_yaw": 0.0}] * 3,
    }
    content = json.dumps(data).encode()
    entries = {}
    for i in range(count):
        name = recording_id("maker/demo-moves", f"data/Joy Dance {i}.json")
        (directory / f"{name}.json").write_bytes(content)
        entries[name] = {
            "title": f"Joy Dance {i}",
            "description": "A joyful dance",
            "repo": "maker/demo-moves",
            "revision": "a" * 40,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    (tmp_path / "community-manifest.json").write_text(json.dumps({"moves": entries}))
    return entries


def test_community_library_loads_lazily_searches_pages_and_verifies_each_recording(tmp_path):
    pytest.importorskip("reachy_mini")
    entries = community_fixture(tmp_path, 25)
    library = MotionLibrary(tmp_path)
    assert library.capabilities()["community_count"] == 25
    page = library.search("joy dance")
    assert len(page["moves"]) == 20 and page["next_offset"] == 20
    assert len(library.search("joy", offset=20)["moves"]) == 5
    assert not library.search("not_a_move")["moves"]
    selected = page["moves"][0]
    assert selected["action"] == "recording"
    name = selected["name"]
    sequence = library.sequence(RobotMotionRequest(action="recording", name=name), Frame.neutral())
    assert sequence.label == f"recording:{name}"
    assert sequence.duration < 24
    (tmp_path / "community" / f"{name}.json").write_text("{}")
    with pytest.raises(ValueError, match="integrity"):
        library.sequence(RobotMotionRequest(action="recording", name=name), Frame.neutral())
    assert entries[name]["revision"] == "a" * 40


@pytest.mark.asyncio
async def test_model_can_search_all_moves_without_tool_result_overflow(settings, tmp_path):
    community_fixture(tmp_path, 45)
    ctx = AppContext(settings=settings)
    library = MotionLibrary(tmp_path)
    ctx.robot.movement_library = library
    ctx.robot.motion_capabilities = library.capabilities
    try:
        registry = ToolRegistry(handlers={"robot_motion": ctx._robot_motion})
        capabilities = await registry.execute("robot_motion", {"action": "capabilities"}, call_id="caps")
        assert capabilities["community_count"] == 45 and "community" not in capabilities
        page = await registry.execute(
            "robot_motion", {"action": "list", "query": "joy", "offset": 20}, call_id="page2"
        )
        assert page["total"] == 45 and page["next_offset"] == 40
        assert len(page["moves"]) == 20
    finally:
        await ctx.stop()
