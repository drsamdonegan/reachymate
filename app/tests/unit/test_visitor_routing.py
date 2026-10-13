import pytest

from app.services.routing.models import RouteRequest, RoutingContext
from app.services.routing.router import VisitorRouter


class Skill:
    def __init__(self):
        self.calls = []

    async def notify(self, payload, idempotency_key):
        self.calls.append((payload, idempotency_key))
        return {"request_ref": payload["request_ref"], "status": "pending"}

    async def status(self, ref):
        return {"request_ref": ref, "status": "host_coming"}


@pytest.mark.asyncio
async def test_duplicate_route_is_one_notification():
    skill = Skill()
    rows = []
    router = VisitorRouter(skill, record=lambda **row: rows.append(row))
    context = RoutingContext(device_id="device", session_ref="session", track_ref="track")
    request = RouteRequest(intent="guest_of_member", visitor_first_name="Alex", host_name="Sam")
    first = await router.route(request, context, "call")
    assert await router.route(request, context, "call") == first
    assert len(skill.calls) == 1
    assert len(rows) == 2  # durable reservation, terminal status
    assert "Sam" not in str(rows) and "Alex" not in str(rows)
    assert first["status"] == "notified"


@pytest.mark.parametrize("name", ["https://evil.test", "@everyone", "Sam123", "Sam\nAdmin"])
def test_names_refuse_injection(name):
    with pytest.raises(ValueError):
        RouteRequest(intent="delivery", visitor_first_name=name)


@pytest.mark.asyncio
async def test_route_cap_and_no_contact_notes_without_local_consent():
    skill = Skill()
    router = VisitorRouter(skill)
    context = RoutingContext(device_id="device", session_ref="session", track_ref="track")
    request = RouteRequest(intent="tour_or_membership", visitor_first_name="Alex", note="https://evil.test")
    await router.route(request, context, "one")
    await router.route(request, context, "two")
    assert (await router.route(request, context, "three"))["status"] == "unavailable"
    assert all("note" not in call[0] for call in skill.calls)


@pytest.mark.asyncio
async def test_durable_write_failure_prevents_notification():
    skill = Skill()

    def broken(**kwargs):
        raise OSError("disk full")

    router = VisitorRouter(skill, record=broken)
    result = await router.route(RouteRequest(intent="other"), RoutingContext("d", "s", "t"), "c")
    assert result["status"] == "unavailable" and not skill.calls
