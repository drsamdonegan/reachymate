"""D1 isolated bench adapter; deployment must supply an independently timed pulse."""

from __future__ import annotations

import asyncio
from typing import Protocol

from app.services.door.base import ReleaseResult, ReleaseStatus


class TimedBenchRelay(Protocol):
    """Implementations must configure local auto-off, including during network loss."""

    async def pulse(self, *, duration_seconds: float) -> bool: ...


class ShellyBenchProvider:
    def __init__(
        self, *, relay: TimedBenchRelay, bench_isolated: bool = False, pulse_seconds: float = 0.5
    ) -> None:
        if not 0 < pulse_seconds <= 1:
            raise ValueError("Bench pulse must be at most one second")
        self.relay = relay
        self.bench_isolated = bench_isolated
        self.pulse_seconds = pulse_seconds

    async def release(self) -> ReleaseResult:
        if not self.bench_isolated:
            return ReleaseResult(ReleaseStatus.REJECTED, "bench_isolation_required")
        try:
            accepted = await asyncio.wait_for(
                self.relay.pulse(duration_seconds=self.pulse_seconds), timeout=2
            )
        except Exception:
            return ReleaseResult(ReleaseStatus.UNCERTAIN, "bench_pulse_outcome_uncertain")
        if not accepted:
            return ReleaseResult(ReleaseStatus.REJECTED, "bench_pulse_rejected")
        return ReleaseResult(ReleaseStatus.ACCEPTED_UNCONFIRMED, "bench_pulse_accepted_unconfirmed")
