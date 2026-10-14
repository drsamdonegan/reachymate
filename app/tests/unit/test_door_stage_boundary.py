from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.services.door.base import (
    DoorActuator,
    DoorPolicy,
    DoorStage,
    ReleaseRequest,
    ReleaseResult,
    ReleaseStatus,
)
from app.services.door.door_sensor import DoorReading, DoorState
from app.services.door.kill_switch import KillSwitch
from app.services.door.reconciler import Reconciler
from app.storage.db import create_session_factory, init_db
from app.storage.release_attempts import ReleaseAttemptRepository
from app.storage.tables import AccessEventRecord


class Provider:
    def __init__(self, status=ReleaseStatus.ACCEPTED_UNCONFIRMED):
        self.calls = 0
        self.status = status

    async def release(self):
        self.calls += 1
        return ReleaseResult(self.status, "provider_result")


class Sensor:
    def __init__(self):
        self.reading = DoorReading("lock", DoorState.CLOSED, datetime.now(timezone.utc), authenticated=True)

    def latest(self):
        return self.reading


def approved_policy(tmp_path, *, stage=DoorStage.D2):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    payload = {
        "kind": "door_stage",
        "stage": stage.name,
        "device_id": "device",
        "site_id": "site",
        "lock_id": "lock",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "pad_validated": True,
        "sensor_installed": True,
        "installer_signoff": True,
        "pia_approved": True,
        "second_factor_required": True,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    document = {"approval": payload, "signature": base64.b64encode(key.sign(canonical)).decode()}
    path = tmp_path / "door-stage-D2.md"
    path.write_text("# Owner approval\n```json\n" + json.dumps(document) + "\n```\n")
    return DoorPolicy(
        stage=stage,
        enabled=True,
        device_id="device",
        site_id="site",
        lock_id="lock",
        approval_path=path,
        approval_public_key=public,
    ), path


def attempts_for(settings):
    factory = create_session_factory(settings)
    init_db(factory)
    with factory() as session:
        for event_id in ("event-1", "event-2"):
            session.add(
                AccessEventRecord(
                    id=event_id,
                    booking_status="ACTIVE",
                    decision="ALLOW",
                    reason_code="ALLOW_ACTIVE_BOOKING",
                    metadata_json={},
                )
            )
        session.commit()
    return ReleaseAttemptRepository(factory)


@pytest.mark.asyncio
async def test_unsigned_approval_refuses_before_provider(settings, tmp_path):
    policy, path = approved_policy(tmp_path)
    path.write_text("# Approved")
    provider = Provider()
    actuator = DoorActuator(
        policy=policy,
        provider=provider,
        attempts=attempts_for(settings),
        kill_switch=KillSwitch(),
        sensor=Sensor(),
    )
    result = await actuator.request_release(
        ReleaseRequest(
            "event-1",
            policy_allowed=True,
            exactly_one_face=True,
            second_factor_verified=True,
            pad_passed=True,
            attended=True,
        )
    )
    assert result.status == ReleaseStatus.REJECTED
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_accepted_is_not_confirmed_and_duplicates_never_retry(settings, tmp_path):
    policy, _ = approved_policy(tmp_path)
    provider = Provider()
    attempts = attempts_for(settings)
    actuator = DoorActuator(
        policy=policy, provider=provider, attempts=attempts, kill_switch=KillSwitch(), sensor=Sensor()
    )
    request = ReleaseRequest(
        "event-1",
        policy_allowed=True,
        exactly_one_face=True,
        second_factor_verified=True,
        pad_passed=True,
        attended=True,
    )
    first = await actuator.request_release(request)
    duplicate = await actuator.request_release(request)
    assert first.status == ReleaseStatus.ACCEPTED_UNCONFIRMED
    assert not first.opening_confirmed
    assert duplicate.status == ReleaseStatus.REJECTED
    assert provider.calls == 1
    assert attempts.get("event-1").status == "ACCEPTED_UNCONFIRMED"


@pytest.mark.asyncio
async def test_uncertain_blocks_future_releases_and_survives_restart(settings, tmp_path):
    policy, _ = approved_policy(tmp_path)
    provider = Provider(ReleaseStatus.UNCERTAIN)
    attempts = attempts_for(settings)
    actuator = DoorActuator(
        policy=policy, provider=provider, attempts=attempts, kill_switch=KillSwitch(), sensor=Sensor()
    )
    result = await actuator.request_release(
        ReleaseRequest(
            "event-1",
            policy_allowed=True,
            exactly_one_face=True,
            second_factor_verified=True,
            pad_passed=True,
            attended=True,
        )
    )
    assert result.status == ReleaseStatus.UNCERTAIN
    restarted = DoorActuator(
        policy=policy, provider=provider, attempts=attempts, kill_switch=KillSwitch(), sensor=Sensor()
    )
    next_result = await restarted.request_release(
        ReleaseRequest(
            "event-2",
            policy_allowed=True,
            exactly_one_face=True,
            second_factor_verified=True,
            pad_passed=True,
            attended=True,
        )
    )
    assert next_result.status == ReleaseStatus.REJECTED
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_missing_durable_event_and_kill_switch_refuse(settings, tmp_path):
    policy, _ = approved_policy(tmp_path)
    provider = Provider()
    kill_switch = KillSwitch()
    actuator = DoorActuator(
        policy=policy,
        provider=provider,
        attempts=attempts_for(settings),
        kill_switch=kill_switch,
        sensor=Sensor(),
    )
    assert (
        await actuator.request_release(
            ReleaseRequest(
                "missing",
                policy_allowed=True,
                exactly_one_face=True,
                second_factor_verified=True,
                pad_passed=True,
                attended=True,
            )
        )
    ).status == ReleaseStatus.REJECTED
    kill_switch.activate()
    assert (
        await actuator.request_release(
            ReleaseRequest(
                "event-1",
                policy_allowed=True,
                exactly_one_face=True,
                second_factor_verified=True,
                pad_passed=True,
                attended=True,
            )
        )
    ).status == ReleaseStatus.REJECTED
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_sensor_confirmation_bound_to_door_and_attempt_time(settings, tmp_path):
    policy, _ = approved_policy(tmp_path)
    attempts = attempts_for(settings)
    sensor = Sensor()
    actuator = DoorActuator(
        policy=policy, provider=Provider(), attempts=attempts, kill_switch=KillSwitch(), sensor=sensor
    )
    await actuator.request_release(
        ReleaseRequest(
            "event-1",
            policy_allowed=True,
            exactly_one_face=True,
            second_factor_verified=True,
            pad_passed=True,
            attended=True,
        )
    )
    sensor.reading = DoorReading("other-door", DoorState.OPEN, datetime.now(timezone.utc), authenticated=True)
    reconciler = Reconciler(attempts=attempts, sensor=sensor)
    assert not reconciler.reconcile("event-1")
    sensor.reading = DoorReading("lock", DoorState.OPEN, datetime.now(timezone.utc), authenticated=True)
    assert reconciler.reconcile("event-1")
    assert attempts.get("event-1").status == "ACTUATED_CONFIRMED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition", ["missing_pad", "missing_factor", "multiple_faces", "unattended", "signature_tampered"]
)
async def test_release_prerequisites_fail_closed(settings, tmp_path, condition):
    from dataclasses import replace

    policy, path = approved_policy(tmp_path)
    request = ReleaseRequest(
        "event-1",
        policy_allowed=True,
        exactly_one_face=True,
        second_factor_verified=True,
        pad_passed=True,
        attended=True,
    )
    if condition == "missing_pad":
        request = replace(request, pad_passed=False)
    elif condition == "missing_factor":
        request = replace(request, second_factor_verified=False)
    elif condition == "multiple_faces":
        request = replace(request, exactly_one_face=False)
    elif condition == "unattended":
        request = replace(request, attended=False)
    else:
        path.write_text(path.read_text().replace('"site_id": "site"', '"site_id": "different"'))
    provider = Provider()
    actuator = DoorActuator(
        policy=policy,
        provider=provider,
        attempts=attempts_for(settings),
        kill_switch=KillSwitch(),
        sensor=Sensor(),
    )
    assert (await actuator.request_release(request)).status == ReleaseStatus.REJECTED
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_durable_sending_row_survives_cancellation(settings, tmp_path):
    policy, _ = approved_policy(tmp_path)
    attempts = attempts_for(settings)
    started = asyncio.Event()

    class BlockedProvider:
        async def release(self):
            started.set()
            await asyncio.Event().wait()

    actuator = DoorActuator(
        policy=policy,
        provider=BlockedProvider(),
        attempts=attempts,
        kill_switch=KillSwitch(),
        sensor=Sensor(),
    )
    task = asyncio.create_task(
        actuator.request_release(
            ReleaseRequest(
                "event-1",
                policy_allowed=True,
                exactly_one_face=True,
                second_factor_verified=True,
                pad_passed=True,
                attended=True,
            )
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts.get("event-1").status == "UNCERTAIN"


@pytest.mark.asyncio
async def test_unsigned_or_wrong_session_host_approval_cannot_release_guest(settings, tmp_path):
    policy, _ = approved_policy(tmp_path, stage=DoorStage.D3)
    provider = Provider()
    actuator = DoorActuator(
        policy=policy,
        provider=provider,
        attempts=attempts_for(settings),
        kill_switch=KillSwitch(),
        sensor=Sensor(),
    )
    request = ReleaseRequest(
        "event-1",
        policy_allowed=True,
        exactly_one_face=True,
        second_factor_verified=True,
        pad_passed=True,
        visitor_kind="guest",
        session_ref="visitor-session",
        host_approval={"approval": {"host_ref": "admin"}},
    )
    assert (await actuator.request_release(request)).status == ReleaseStatus.REJECTED
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_vendor_claim_cannot_replace_authenticated_sensor(settings, tmp_path):
    policy, _ = approved_policy(tmp_path)
    actuator = DoorActuator(
        policy=policy,
        provider=Provider(ReleaseStatus.ACTUATED_CONFIRMED),
        attempts=attempts_for(settings),
        kill_switch=KillSwitch(),
        sensor=Sensor(),
    )
    result = await actuator.request_release(
        ReleaseRequest(
            "event-1",
            policy_allowed=True,
            exactly_one_face=True,
            second_factor_verified=True,
            pad_passed=True,
            attended=True,
        )
    )
    assert result.status == ReleaseStatus.ACCEPTED_UNCONFIRMED
    assert not result.opening_confirmed


@pytest.mark.asyncio
async def test_separate_actuator_instances_cannot_release_same_lock_concurrently(settings, tmp_path):
    policy, _ = approved_policy(tmp_path)
    first_repo = attempts_for(settings)
    second_repo = ReleaseAttemptRepository(first_repo.session_factory)
    provider = Provider()
    first = DoorActuator(
        policy=policy, provider=provider, attempts=first_repo, kill_switch=KillSwitch(), sensor=Sensor()
    )
    second = DoorActuator(
        policy=policy, provider=provider, attempts=second_repo, kill_switch=KillSwitch(), sensor=Sensor()
    )
    results = await asyncio.gather(
        first.request_release(
            ReleaseRequest(
                "event-1",
                policy_allowed=True,
                exactly_one_face=True,
                second_factor_verified=True,
                pad_passed=True,
                attended=True,
            )
        ),
        second.request_release(
            ReleaseRequest(
                "event-2",
                policy_allowed=True,
                exactly_one_face=True,
                second_factor_verified=True,
                pad_passed=True,
                attended=True,
            )
        ),
    )
    assert len([result for result in results if result.status == ReleaseStatus.ACCEPTED_UNCONFIRMED]) == 1
    assert provider.calls == 1
