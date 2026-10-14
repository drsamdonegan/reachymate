from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from app.models.enums import BookingStatus
from app.services.slack.booking_service import MlaiBookingClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (200, {"ready": True}, True),
        (200, {"status": "healthy"}, True),
        (200, {"status": "starting"}, False),
        (503, {"ready": False}, False),
    ],
)
async def test_booking_healthcheck_probes_backend_readiness(
    settings, status_code: int, payload: dict, expected: bool
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/healthz/ready"
        return httpx.Response(status_code, json=payload)

    client = MlaiBookingClient(settings, transport=httpx.MockTransport(handler))
    assert await client.healthcheck() is expected
    await client.close()


@pytest.mark.asyncio
async def test_booking_client_detects_booked_today(settings) -> None:
    now = datetime(2026, 1, 1, 13, 30, tzinfo=timezone.utc)
    app = FastAPI()

    @app.get("/api/v1/points/coworking/my-bookings/")
    async def my_bookings(slack_user_id: str) -> list[dict]:
        assert slack_user_id == "U123"
        return [{"id": "booking-1", "date": "2026-01-02", "status": "booked"}]

    client = MlaiBookingClient(settings, transport=httpx.ASGITransport(app=app))
    result = await client.get_active_booking(slack_user_id="U123", now=now)
    await client.close()

    assert result.active is True
    assert result.status == BookingStatus.ACTIVE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("now", "booking_date"),
    [
        (datetime(2026, 4, 4, 12, 59, tzinfo=timezone.utc), "2026-04-04"),
        (datetime(2026, 4, 4, 13, 1, tzinfo=timezone.utc), "2026-04-05"),
        (datetime(2026, 10, 3, 13, 59, tzinfo=timezone.utc), "2026-10-03"),
        (datetime(2026, 10, 3, 14, 1, tzinfo=timezone.utc), "2026-10-04"),
    ],
)
async def test_booking_client_uses_melbourne_date_across_dst_boundaries(
    settings, now: datetime, booking_date: str
) -> None:
    app = FastAPI()

    @app.get("/api/v1/points/coworking/my-bookings/")
    async def my_bookings(slack_user_id: str) -> list[dict]:
        return [{"id": "booking-1", "date": booking_date, "status": "booked"}]

    client = MlaiBookingClient(settings, transport=httpx.ASGITransport(app=app))
    result = await client.get_active_booking(slack_user_id="U123", now=now)
    await client.close()

    assert result.status == BookingStatus.ACTIVE


@pytest.mark.asyncio
async def test_booking_client_fails_closed_on_malformed_payload(settings) -> None:
    app = FastAPI()

    @app.get("/api/v1/points/coworking/my-bookings/")
    async def my_bookings(slack_user_id: str) -> dict:
        return {"unexpected": "shape"}

    client = MlaiBookingClient(settings, transport=httpx.ASGITransport(app=app))
    result = await client.get_active_booking(
        slack_user_id="U123",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    await client.close()

    assert result.active is False
    assert result.status == BookingStatus.LOOKUP_FAILED


@pytest.mark.asyncio
async def test_booking_client_handles_lookup_failure(settings) -> None:
    app = FastAPI()

    @app.get("/api/v1/points/coworking/my-bookings/")
    async def my_bookings(slack_user_id: str):  # type: ignore[no-untyped-def]
        return httpx.Response(status_code=500)

    client = MlaiBookingClient(settings, transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    result = await client.get_active_booking(slack_user_id="U123", now=datetime.now(timezone.utc))
    await client.close()

    assert result.active is False
    assert result.status == BookingStatus.LOOKUP_FAILED
