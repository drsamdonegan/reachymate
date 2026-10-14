from __future__ import annotations

import asyncio

import pytest

from app.models.enums import Decision
from app.runtime import AppContext, FakeIdentificationService
from app.services.slack.booking_service import MockBookingClient


def build_context(settings, known_face_result, *, identification=None, booking=None) -> AppContext:
    context = AppContext(
        settings=settings,
        identification_service=identification or FakeIdentificationService(known_face_result),
        booking_client=booking or MockBookingClient(booked_today={"U123"}),
    )
    context.users.upsert(
        user_id="user-1",
        full_name="Test Member",
        slack_user_id="U123",
        slack_display_name="member",
        active=True,
        biometric_consent=True,
        access_revoked=False,
    )
    return context


class FailingIdentification(FakeIdentificationService):
    async def identify(self):  # type: ignore[no-untyped-def]
        raise RuntimeError("detector failed")


class FailingBooking(MockBookingClient):
    async def get_active_booking(self, **kwargs):  # type: ignore[no-untyped-def]
        raise TimeoutError("booking timed out")


class BlockingIdentification(FakeIdentificationService):
    def __init__(self, result) -> None:  # type: ignore[no-untyped-def]
        super().__init__(result)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def identify(self):  # type: ignore[no-untyped-def]
        self.entered.set()
        await self.release.wait()
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["detector", "booking", "robot", "database"])
async def test_attempt_exceptions_recover_and_write_terminal_event(
    settings, known_face_result, failure: str
) -> None:
    identification = (
        FailingIdentification(known_face_result)
        if failure == "detector"
        else FakeIdentificationService(known_face_result)
    )
    booking = FailingBooking() if failure == "booking" else MockBookingClient(booked_today={"U123"})
    context = build_context(
        settings,
        known_face_result,
        identification=identification,
        booking=booking,
    )
    if failure == "robot":

        async def fail_animation() -> None:
            raise RuntimeError("robot failed")

        context.robot.animate_ack = fail_animation  # type: ignore[method-assign]
    if failure == "database":

        def fail_user_lookup(_user_id: str):
            raise RuntimeError("database failed")

        context.users.get = fail_user_lookup  # type: ignore[method-assign]

    await context.start()
    try:
        await context.state_machine.handle_wake_word()
        event = context.recent_events(limit=1)[0]
    finally:
        await context.stop()

    assert event["decision"] == Decision.ERROR.value
    assert event["reason_code"] == "ERROR_INTERNAL"
    assert context.state_machine.state.value == "LISTENING_LOW_POWER"
    assert context.guard.is_engaged() is False


@pytest.mark.asyncio
async def test_cancelled_attempt_recovers_and_writes_terminal_event(settings, known_face_result) -> None:
    identification = BlockingIdentification(known_face_result)
    context = build_context(
        settings,
        known_face_result,
        identification=identification,
    )
    await context.start()
    try:
        task = asyncio.create_task(context.state_machine.handle_wake_word())
        await identification.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        event = context.recent_events(limit=1)[0]
    finally:
        await context.stop()

    assert event["decision"] == Decision.ERROR.value
    assert context.state_machine.state.value == "LISTENING_LOW_POWER"
    assert context.guard.is_engaged() is False


@pytest.mark.asyncio
async def test_concurrent_wake_is_single_flight(settings, known_face_result) -> None:
    identification = BlockingIdentification(known_face_result)
    context = build_context(
        settings,
        known_face_result,
        identification=identification,
    )
    await context.start()
    try:
        first = asyncio.create_task(context.state_machine.handle_wake_word())
        await identification.entered.wait()
        second = asyncio.create_task(context.state_machine.handle_wake_word())
        await second
        identification.release.set()
        await first
        events = context.recent_events(limit=10)
    finally:
        await context.stop()

    assert len(events) == 1
