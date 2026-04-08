from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from app.models.enums import BookingStatus
from app.services.slack.booking_service import MlaiBookingClient


@pytest.mark.asyncio
async def test_booking_client_detects_booked_today(settings) -> None:
    app = FastAPI()

    @app.get("/api/v1/points/coworking/my-bookings/")
    async def my_bookings(slack_user_id: str) -> list[dict]:
        assert slack_user_id == "U123"
        return [{"id": "booking-1", "date": datetime.now(timezone.utc).date().isoformat(), "status": "booked"}]

    client = MlaiBookingClient(settings, transport=httpx.ASGITransport(app=app))
    result = await client.get_active_booking(slack_user_id="U123", now=datetime.now(timezone.utc))
    await client.close()

    assert result.active is True
    assert result.status == BookingStatus.ACTIVE


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
