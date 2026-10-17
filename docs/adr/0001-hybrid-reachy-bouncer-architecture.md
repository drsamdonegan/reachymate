# ADR 0001: Hybrid local/cloud Reachy bouncer architecture

- Status: accepted for implementation; physical actuation remains unapproved
- Date: 2026-08-30

This records the original boundary decision. The current attended implementation uses native GPT-Live with constrained movement and on-request vision, described in [the architecture](../architecture.md) and [GPT-Live guide](../gpt-live.md). The active door boundary remains a non-actuating placeholder; no fixed physical lock is composed by the demo. Explicit attended launch or dashboard opening is an additional opt-in cloud-audio trigger.

## Decision

Reachy uses a hybrid architecture with hard trust boundaries:

1. A local, always-on 16 kHz audio path detects the wake phrase. It opens no cloud connection while idle.
2. An engaged session uses OpenAI Realtime speech-to-speech (`gpt-realtime-2.1` at this decision date) for low-latency, interruptible conversation.
3. Roo supplies persona guidance and public MLAI answers through one organization-bound, read-only public-knowledge tool. Roo cannot receive biometric data or door credentials.
4. A deterministic local access controller owns identity eligibility, booking policy, health checks, the durable attempt, and the fixed SALTO lock target.
5. A model or spoken command may request an access attempt and narrate its terminal result. It cannot authorize or actuate the door.

The interaction service, access controller, and private administration plane are separate capabilities even when initially deployed in one Python process.

## Safety invariants

- No durable access attempt, no actuator call.
- No explicit production runtime and `PHYSICAL_UNLOCK_ENABLED=true`, no physical actuator call.
- Exactly one live, consenting, active, non-revoked member or deny.
- No current authoritative booking/policy for the configured member, site, lock, and local date/time, no unlock.
- Every required health value must be present, fresh, and exactly `HEALTHY`; otherwise access is denied.
- Any exception, timeout, cancellation, malformed response, uncertainty, or unlock failure ends in `DENY`/`ERROR`, never `ALLOW`.
- Chat availability cannot make access ready. Access failure need not disable safe public chat.
- Simulation returns `simulated`, never `unlocked`.
- Site ID, lock ID, identity, booking result, and policy inputs never come from model-generated tool arguments.

## Consequences

This adds explicit adapters and more tests, but prevents a prompt injection, voice false accept, Roo error, or Realtime failure from crossing the door-control boundary. Local wake preserves idle privacy and cost. Cloud voice remains optional for access correctness.

## Approval gate

The physical flag stays false until the coworking-space owner and access/security owner approve the policy matrix, threat model, privacy/consent handling, manual fallback, SALTO integration, and pilot evidence.
