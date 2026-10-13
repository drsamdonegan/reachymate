"""Future staged door boundary. Runtime intentionally does not compose it."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.services.door.door_sensor import DoorSensor, DoorState
from app.services.door.kill_switch import KillSwitch
from app.storage.release_attempts import ReleaseAttemptRepository


class DoorStage(IntEnum):
    D0 = 0
    D1 = 1
    D2 = 2
    D3 = 3
    D4 = 4


class ReleaseStatus(str, Enum):
    ACTUATED_CONFIRMED = "ACTUATED_CONFIRMED"
    ACCEPTED_UNCONFIRMED = "ACCEPTED_UNCONFIRMED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class ReleaseResult:
    status: ReleaseStatus
    detail: str
    provider_request_id: str | None = None

    @property
    def opening_confirmed(self) -> bool:
        return self.status == ReleaseStatus.ACTUATED_CONFIRMED


@dataclass(frozen=True)
class ReleaseRequest:
    access_event_id: str
    policy_allowed: bool = False
    second_factor_verified: bool = False
    pad_passed: bool = False
    exactly_one_face: bool = False
    attended: bool = False
    visitor_kind: str = "member"
    session_ref: str = ""
    host_approval: dict[str, Any] | None = None


class ReleaseProvider(Protocol):
    async def release(self) -> ReleaseResult: ...


def verify_signed_document(document: dict[str, Any], public_key: bytes) -> dict[str, Any]:
    payload = document["approval"]
    if not isinstance(payload, dict) or not public_key:
        raise ValueError("Signed approval is missing")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    signature = base64.b64decode(document["signature"], validate=True)
    Ed25519PublicKey.from_public_bytes(public_key).verify(signature, canonical)
    return payload


def _aware_date(value: Any) -> datetime:
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Approval timestamp must be timezone aware")
    return result


@dataclass(frozen=True)
class DoorPolicy:
    stage: DoorStage = DoorStage.D0
    enabled: bool = False
    device_id: str = ""
    site_id: str = ""
    lock_id: str = ""
    approval_path: Path | None = None
    approval_public_key: bytes | None = None
    host_public_keys: dict[str, bytes] = field(default_factory=dict)
    bench_isolated: bool = False

    def verify(self, request: ReleaseRequest) -> str | None:
        if not self.enabled or self.stage == DoorStage.D0:
            return "physical_unlock_disabled"
        if not request.policy_allowed or not request.exactly_one_face:
            return "policy_refused"
        if not all((self.device_id, self.site_id, self.lock_id)):
            return "door_bindings_missing"
        try:
            if self.approval_path is None or self.approval_public_key is None:
                raise ValueError("Approval is unavailable")
            source = self.approval_path.read_text()
            if len(source) > 16_000:
                raise ValueError("Approval is too large")
            if source.lstrip().startswith("{"):
                document = json.loads(source)
            else:
                fenced = re.search(r"```json\s+(\{.*?\})\s*```", source, re.S)
                if fenced is None:
                    raise ValueError("Approval signature document is missing")
                document = json.loads(fenced.group(1))
            approval = verify_signed_document(document, self.approval_public_key)
            expected = {
                "kind": "door_stage",
                "stage": self.stage.name,
                "device_id": self.device_id,
                "site_id": self.site_id,
                "lock_id": self.lock_id,
            }
            if any(approval.get(key) != value for key, value in expected.items()):
                raise ValueError("Approval bindings differ")
            if _aware_date(approval["expires_at"]) <= datetime.now(timezone.utc):
                raise ValueError("Approval expired")
            if self.stage == DoorStage.D1:
                if not self.bench_isolated or approval.get("bench_isolated") is not True:
                    return "bench_isolation_required"
            else:
                if not request.pad_passed:
                    return "pad_required"
                requirements = ("pad_validated", "sensor_installed", "installer_signoff", "pia_approved")
                if not all(approval.get(key) is True for key in requirements):
                    return "stage_prerequisites_missing"
                require_factor = (
                    self.stage <= DoorStage.D3 or approval.get("second_factor_required") is not False
                )
                if not require_factor and approval.get("g6_validated") is not True:
                    return "g6_required_to_drop_factor"
                if require_factor and not request.second_factor_verified:
                    return "second_factor_required"
                if self.stage == DoorStage.D2 and not request.attended:
                    return "attended_staff_required"
                if request.visitor_kind == "guest":
                    if self.stage < DoorStage.D3 or not self._guest_approved(request):
                        return "host_approval_required"
                elif request.visitor_kind != "member":
                    return "visitor_kind_refused"
        except Exception:
            return "invalid_signed_stage_approval"
        return None

    def _guest_approved(self, request: ReleaseRequest) -> bool:
        try:
            document = request.host_approval or {}
            host = document["approval"]["host_ref"]
            key = self.host_public_keys[host]
            approval = verify_signed_document(document, key)
            expected = {
                "kind": "guest_entry",
                "device_id": self.device_id,
                "lock_id": self.lock_id,
                "session_ref": request.session_ref,
                "access_event_id": request.access_event_id,
            }
            if any(approval.get(k) != v for k, v in expected.items()):
                return False
            now = datetime.now(timezone.utc)
            return 0 <= (
                now - _aware_date(approval["issued_at"])
            ).total_seconds() <= 300 and now < _aware_date(approval["expires_at"])
        except Exception:
            return False


class DoorActuator:
    """Reserve durably, send at most once, and block a lock until reconciliation."""

    actuates_hardware = True
    is_placeholder = False

    def __init__(
        self,
        *,
        policy: DoorPolicy,
        provider: ReleaseProvider,
        attempts: ReleaseAttemptRepository,
        kill_switch: KillSwitch,
        sensor: DoorSensor | None = None,
        timeout_seconds: float = 3,
    ) -> None:
        self.policy = policy
        self.provider = provider
        self.attempts = attempts
        self.kill_switch = kill_switch
        self.sensor = sensor
        self.timeout_seconds = timeout_seconds
        self._lock = asyncio.Lock()

    def _refusal(self, request: ReleaseRequest) -> str | None:
        if self.kill_switch.active:
            return "kill_switch_active"
        refused = self.policy.verify(request)
        if refused:
            return refused
        if self.policy.stage >= DoorStage.D2:
            reading = self.sensor.latest() if self.sensor is not None else None
            if (
                reading is None
                or not reading.fresh(lock_id=self.policy.lock_id)
                or reading.state != DoorState.CLOSED
            ):
                return "fresh_closed_door_sensor_required"
        return None

    async def request_release(self, request: ReleaseRequest) -> ReleaseResult:
        async with self._lock:
            refused = self._refusal(request)
            if refused:
                return ReleaseResult(ReleaseStatus.REJECTED, refused)
            row, reservation = await asyncio.to_thread(
                self.attempts.reserve,
                access_event_id=request.access_event_id,
                device_id=self.policy.device_id,
                site_id=self.policy.site_id,
                lock_id=self.policy.lock_id,
                stage=self.policy.stage.name,
            )
            if reservation != "reserved" or row is None:
                return ReleaseResult(ReleaseStatus.REJECTED, reservation)
            refused = self._refusal(request)
            if refused:
                await asyncio.to_thread(
                    self.attempts.update, request.access_event_id, status="REJECTED", detail=refused
                )
                return ReleaseResult(ReleaseStatus.REJECTED, refused)
            try:
                # Commit SENDING before I/O; a crash here remains an unresolved lock.
                await asyncio.to_thread(
                    self.attempts.update,
                    request.access_event_id,
                    status="SENDING",
                    detail="release_send_started",
                )
            except Exception:
                return ReleaseResult(ReleaseStatus.REJECTED, "durable_send_marker_failed")
            try:
                result = await asyncio.wait_for(self.provider.release(), timeout=self.timeout_seconds)
                if result.status == ReleaseStatus.ACTUATED_CONFIRMED:
                    # A provider response alone cannot establish physical movement.
                    result = ReleaseResult(
                        ReleaseStatus.ACCEPTED_UNCONFIRMED,
                        "provider_confirmation_requires_evidence",
                        result.provider_request_id,
                    )
            except asyncio.CancelledError:
                await asyncio.to_thread(
                    self.attempts.update,
                    request.access_event_id,
                    status="UNCERTAIN",
                    detail="cancelled_after_send_started",
                )
                raise
            except Exception:
                result = ReleaseResult(ReleaseStatus.UNCERTAIN, "transport_outcome_uncertain")
            try:
                await asyncio.to_thread(
                    self.attempts.update,
                    request.access_event_id,
                    status=result.status.value,
                    detail=result.detail,
                    provider_request_id=result.provider_request_id,
                )
            except Exception:
                # Existing durable SENDING remains blocking, even if this write fails.
                return ReleaseResult(ReleaseStatus.UNCERTAIN, "result_persistence_failed")
            return result
