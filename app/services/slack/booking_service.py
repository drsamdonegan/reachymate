"""Booking lookup via mlai-backend."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.models.domain import BookingResult
from app.models.enums import BookingStatus
from app.services.slack.schemas import BookingHelpRequest, CoworkingBooking


class BookingClient(Protocol):
    async def get_active_booking(self, *, slack_user_id: str, now: datetime) -> BookingResult: ...
    async def send_booking_help(self, request: BookingHelpRequest) -> bool: ...


class MlaiBookingClient:
    """HTTP client for mlai-backend coworking bookings."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.mlai_backend_url.rstrip("/"),
            timeout=5.0,
            headers={"X-API-Key": settings.mlai_backend_api_key},
            transport=transport,
        )
        self.timezone = ZoneInfo(settings.office_timezone)

    async def close(self) -> None:
        await self.client.aclose()

    async def get_active_booking(self, *, slack_user_id: str, now: datetime) -> BookingResult:
        if not slack_user_id:
            return BookingResult(status=BookingStatus.USER_NOT_LINKED, active=False, detail="Missing slack_user_id")

        try:
            response = await self.client.get(
                "/api/v1/points/coworking/my-bookings/",
                params={"slack_user_id": slack_user_id},
            )
            response.raise_for_status()
        except Exception as exc:
            return BookingResult(
                status=BookingStatus.LOOKUP_FAILED,
                active=False,
                detail=f"Booking lookup failed: {exc}",
            )

        today = now.astimezone(self.timezone).date()
        bookings = [CoworkingBooking.model_validate(row) for row in response.json()]
        for booking in bookings:
            if booking.status == "booked" and booking.date == today:
                return BookingResult(
                    status=BookingStatus.ACTIVE,
                    active=True,
                    booking_date=booking.date,
                    source_booking_id=booking.id,
                    metadata={"bookings_count": len(bookings)},
                )
        return BookingResult(status=BookingStatus.NONE, active=False, metadata={"bookings_count": len(bookings)})

    async def send_booking_help(self, request: BookingHelpRequest) -> bool:
        try:
            response = await self.client.post(
                "/api/v1/points/coworking/booking-help/",
                json=request.model_dump(),
            )
            return response.status_code < 400
        except Exception:
            return False


class MockBookingClient:
    """In-memory booking client used in tests and simulation."""

    def __init__(self, booked_today: set[str] | None = None, fail_lookup: bool = False) -> None:
        self.booked_today = booked_today or set()
        self.fail_lookup = fail_lookup
        self.help_requests: list[dict[str, str]] = []

    async def get_active_booking(self, *, slack_user_id: str, now: datetime) -> BookingResult:
        if self.fail_lookup:
            return BookingResult(status=BookingStatus.LOOKUP_FAILED, active=False, detail="mock failure")
        if slack_user_id in self.booked_today:
            return BookingResult(status=BookingStatus.ACTIVE, active=True, booking_date=now.date(), source_booking_id="mock")
        return BookingResult(status=BookingStatus.NONE, active=False)

    async def send_booking_help(self, request: BookingHelpRequest) -> bool:
        self.help_requests.append(request.model_dump())
        return True
