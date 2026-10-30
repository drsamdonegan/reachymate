from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.admin_api import router
from app.services.events.checkin import EventCheckin
from app.services.events.luma import LumaClient, LumaError
from app.services.events.qr import QRDecoder, parse_ticket
from app.services.system.telemetry import TelemetryBroker

QR = "https://luma.com/check-in/evt-demo?pk=g-synthetic"
STAMP = "2026-10-30T23:01:00Z"


def provider_guest(checked=None, *, status="approved"):
    return {
        "id": "gst-synthetic",
        "user_name": "Synthetic Guest",
        "user_email": "private@example.test",
        "approval_status": status,
        "event_tickets": [
            {"id": "ticket-one", "name": "Entry", "checked_in_at": checked, "is_captured": True}
        ],
    }


def service(*, configured=True, handler=None, camera=None, on_stop=None):
    client = LumaClient(
        SecretStr("synthetic-api-key") if configured else None,
        transport=httpx.MockTransport(handler or (lambda _: httpx.Response(200, json=provider_guest()))),
    )
    item = EventCheckin(
        camera=camera,
        client=client,
        telemetry=TelemetryBroker(),
        on_confirmed=AsyncMock(),
        on_scanner_stop=on_stop,
    )
    item.event = {"id": "evt-demo", "name": "Synthetic demo"}
    return item


def allow_lookup(item):
    item._last_lookup = time.monotonic() - 3


@pytest.mark.parametrize(
    "value",
    [
        "http://luma.com/check-in/evt-demo?pk=x",
        "https://luma.com.evil.test/check-in/evt-demo?pk=x",
        "https://luma.com@evil.test/check-in/evt-demo?pk=x",
        "https://luma.com:443/check-in/evt-demo?pk=x",
        "https://luma.com/check-in/evt-other?pk=x",
        "https://luma.com/demo?pk=x",
        "https://luma.com/check-in/evt-demo?pk=x&pk=y",
        "https://luma.com/check-in/evt-demo?pk=",
        "https://luma.com/check-in/evt-demo?pk=%0Aprivate",
        "https://luma.com/check-in/evt-demo?pk=x#fragment",
        "\n" + QR,
        "https://luma.com/check-in/evt-demo/../evt-other?pk=x",
        "x" * 2049,
    ],
)
def test_reject_untrusted_or_wrong_event_codes(value):
    with pytest.raises(ValueError) as error:
        parse_ticket(value, "evt-demo")
    assert value not in str(error.value)


@pytest.mark.parametrize("host,key", [("luma.com", "g-synthetic"), ("lu.ma", "abc123")])
def test_canonical_ticket_and_guest_keys(host, key):
    code = parse_ticket(f"https://{host}/check-in/evt-demo?pk={key}&tracking=ignored", "evt-demo")
    assert code.url == f"https://luma.com/check-in/evt-demo?pk={key}"
    assert key not in repr(code)


def test_decode_actual_qr_pixels():
    cv2 = pytest.importorskip("cv2")
    pixels = cv2.QRCodeEncoder_create().encode(QR)
    frame = cv2.resize(pixels, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    assert QRDecoder().decode(frame) == [QR]
    assert QRDecoder().decode(None) == []


@pytest.mark.asyncio
async def test_scan_is_read_only_duplicate_suppressed_and_only_timestamp_confirms(caplog):
    requests = []
    state = provider_guest()

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=state)

    item = service(handler=handler)
    try:
        first = await item.scan(QR)
        assert first["current"]["status"] == "awaiting_luma"
        await item.scan(QR)
        assert len(requests) == 1
        assert requests[0].method == "GET"
        assert requests[0].url.path == "/v1/events/guests/get"
        assert requests[0].url.params["id"] == "g-synthetic"
        assert requests[0].headers["x-luma-api-key"] == "synthetic-api-key"
        assert "private@example.test" not in json.dumps(first)
        state["event_tickets"][0]["checked_in_at"] = STAMP
        allow_lookup(item)
        verified = await item.verify(first["current"]["scan_id"])
        assert verified["current"]["status"] == "checked_in"
        assert first["current"]["status"] == "awaiting_luma"  # snapshots don't mutate
        item.on_confirmed.assert_awaited_once()
        allow_lookup(item)
        await item.verify(first["current"]["scan_id"])
        item.on_confirmed.assert_awaited_once()
        logged = caplog.text + json.dumps(item.telemetry.snapshot())
        assert not any(value in logged for value in ("g-synthetic", "Synthetic Guest", "synthetic-api-key"))
    finally:
        await item.close()


@pytest.mark.asyncio
async def test_group_and_already_checked_in_are_not_a_new_arrival():
    state = provider_guest(STAMP)
    state["event_tickets"].append(
        {"id": "ticket-two", "name": "Companion", "checked_in_at": None, "is_captured": True}
    )
    item = service(handler=lambda _: httpx.Response(200, json=state))
    try:
        first = await item.scan(QR)
        assert first["current"]["status"] == "partially_checked_in"
        assert len(first["current"]["tickets"]) == 2
        item.on_confirmed.assert_not_awaited()
        allow_lookup(item)
        second = await item.scan(QR.replace("g-synthetic", "another-ticket"))
        assert second["current"]["scan_id"] == first["current"]["scan_id"]
        state["event_tickets"][1]["checked_in_at"] = STAMP
        allow_lookup(item)
        assert (await item.verify(first["current"]["scan_id"]))["current"]["status"] == "checked_in"
        item.on_confirmed.assert_awaited_once()
        await item.next_guest()
        allow_lookup(item)
        assert (await item.scan(QR.replace("g-synthetic", "another-ticket")))["current"][
            "status"
        ] == "checked_in"
        item.on_confirmed.assert_awaited_once()
    finally:
        await item.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending_approval", "waitlist", "declined", "invited"])
async def test_unapproved_registration_never_confirms(status):
    item = service(handler=lambda _: httpx.Response(200, json=provider_guest(STAMP, status=status)))
    try:
        assert (await item.scan(QR))["current"]["status"] == "not_approved"
        item.on_confirmed.assert_not_awaited()
    finally:
        await item.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 302])
async def test_luma_errors_never_confirm_or_leak_body_and_no_redirect(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text="private raw provider detail", headers={"location": "https://evil.test"}
        )

    item = service(handler=handler)
    try:
        result = await item.scan(QR)
        assert result["current"]["status"] == "unverified"
        assert result["error"] and "private" not in result["error"]
        assert len(calls) == 1
        item.on_confirmed.assert_not_awaited()
    finally:
        await item.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("timestamp", [True, "yesterday", "2026-10-30T23:00:00"])
async def test_malformed_provider_timestamps_fail_closed(timestamp):
    item = service(handler=lambda _: httpx.Response(200, json=provider_guest(timestamp)))
    try:
        result = await item.scan(QR)
        assert result["current"]["status"] == "unverified"
        assert result["error"] == "Unexpected Luma ticket response"
    finally:
        await item.close()


@pytest.mark.asyncio
async def test_no_key_handoff_expiry_and_stale_verification():
    item = service(configured=False)
    try:
        first = await item.scan(QR)
        assert not first["verification_available"]
        assert first["current"]["status"] == "unverified"
        assert first["current"]["checkin_url"] == QR
        item.current["seen_at"] -= 301
        assert item.snapshot()["current"] is None
        assert item._code is None and not item._seen
        with pytest.raises(ValueError, match="expired"):
            await item.verify(first["current"]["scan_id"])
    finally:
        await item.close()


@pytest.mark.asyncio
async def test_selection_validates_management_and_in_person_event():
    state = {"id": "evt-next", "name": "Next event", "access": "view", "location_type": "offline"}
    item = service(handler=lambda _: httpx.Response(200, json=state))
    try:
        with pytest.raises(LumaError, match="cannot manage"):
            await item.select_event("evt-next")
        assert item.event["id"] == "evt-demo"
        state.update(access="manage", location_type="zoom")
        with pytest.raises(LumaError, match="in-person"):
            await item.select_event("evt-next")
        state["location_type"] = "offline"
        assert (await item.select_event("evt-next"))["event"]["name"] == "Next event"
    finally:
        await item.close()


@pytest.mark.asyncio
async def test_slow_decode_does_not_block_loop_and_stop_finishes_camera_worker():
    entered, finish = threading.Event(), threading.Event()
    released = AsyncMock()

    def decode(_):
        entered.set()
        assert finish.wait(2)
        return [QR]

    item = service(configured=False, camera=SimpleNamespace(get_frame=lambda: object()), on_stop=released)
    item.decoder = SimpleNamespace(decode=decode)
    await item.start_scanning()
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        stopping = asyncio.create_task(item.stop_scanning())
        await asyncio.sleep(0.02)
        assert not stopping.done()
        released.assert_not_awaited()
        finish.set()
        await asyncio.wait_for(stopping, 1)
        released.assert_awaited_once()
        assert not item.scanning and item.current is None
    finally:
        finish.set()
        await item.close()


@pytest.mark.asyncio
async def test_stop_cancels_slow_provider_and_blocks_late_result():
    entered = asyncio.Event()

    async def handler(_):
        entered.set()
        await asyncio.Event().wait()

    item = service(handler=handler, camera=SimpleNamespace(get_frame=lambda: object()))
    item.decoder = SimpleNamespace(decode=lambda _: [QR])
    await item.start_scanning()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(item.stop_scanning(), 1)
        assert not item.scanning
        assert item.current["status"] == "unverified"
        item.on_confirmed.assert_not_awaited()
    finally:
        await item.close()


@pytest.mark.asyncio
async def test_fault_gate_and_missing_frame_do_not_claim_active_scanner():
    item = service(configured=False, camera=SimpleNamespace(get_frame=lambda: None))
    item.camera_allowed = lambda: False
    try:
        with pytest.raises(ValueError, match="paused"):
            await item.start_scanning()
        item.camera_allowed = lambda: True
        await item.start_scanning()
        await asyncio.wait_for(item._task, 1)
        assert not item.scanning and item.error.startswith("Scanner unavailable")
    finally:
        await item.close()


def test_admin_flow_has_private_responses_and_stale_scan_protection():
    item = service(configured=False)
    app = FastAPI()
    app.include_router(router)
    app.state.context = SimpleNamespace(event_checkin=item)
    with TestClient(app) as client:
        result = client.post("/api/admin/event-checkin/scan", json={"qr_code": QR})
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
        assert result.headers["referrer-policy"] == "no-referrer"
        scan_id = result.json()["current"]["scan_id"]
        client.post("/api/admin/event-checkin/next")
        assert client.post("/api/admin/event-checkin/verify", json={"scan_id": scan_id}).status_code == 409
        client.portal.call(item.close)


@pytest.mark.asyncio
async def test_camera_lease_survives_voice_end_and_releases_after_scanning(settings):
    from app.models.enums import AppState
    from app.runtime import AppContext

    context = AppContext(settings=settings)
    releases = []
    context.reachy_media = SimpleNamespace(release_idle_camera=lambda: releases.append(True))
    context.event_checkin.scanning = True
    context.conversation.session_ref = "synthetic"
    await context._end_engagement()
    assert not releases
    assert context.state_machine.state == AppState.IDLE
    context.event_checkin.scanning = False
    await context._release_event_camera()
    assert releases == [True]
    context.reachy_media = None
    await context.stop()


def test_event_admin_endpoints_absent_in_production(settings):
    from app.main import create_app
    from app.models.enums import RuntimeMode

    settings.app_env = RuntimeMode.PRODUCTION
    client = TestClient(create_app(settings))
    assert client.get("/api/admin/event-checkin").status_code == 404
    assert client.post("/api/admin/event-checkin/start").status_code == 404
