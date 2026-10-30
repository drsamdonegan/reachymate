# Reachy bouncer threat model

## Assets and trust boundaries

Protected assets are physical entry, member identity and consent, face embeddings, booking status, access logs, SALTO/OpenAI/MLAI credentials, and public-knowledge integrity. Trust boundaries exist between the public doorway, Reachy hardware/host, OpenAI voice and Responses, MLAI/Roo, SALTO, the member system, and the private admin plane. These are required controls for deployment; the current local staging demo is not a claim that the production administration plane or physical integration exists.

The public microphone, camera, spoken text, model output, Roo content, and all network responses are untrusted. Only the deterministic access controller may combine verified local identity, authoritative membership/booking data, fresh health, fixed configuration, and a durable event into an actuator request.

## Threats and required controls

| Threat | Required control | Verification |
|---|---|---|
| Anonymous or forged enrollment | Private OIDC/MFA admin plane, role check, authoritative member lookup, recorded consent | Anonymous 401; wrong role 403; arbitrary member binding rejected |
| Stolen admin session | Short sessions, MFA, CSRF/origin checks, audit trail, revocation | Session/CSRF security tests and operator runbook |
| Prompt or tool injection | Constrained typed concierge tools; bounded movement and attended snapshots; no door actuator or identity authority | Adversarial conversation/tool tests produce zero actuator calls |
| Poisoned/fake Roo content | Curated public corpus, source metadata, scoped principal, no policy authority | Corpus provenance review and injection corpus |
| Photo, phone, print, or video replay | Dedicated PAD/liveness model and challenge/fallback; never brightness/blur alone | Held-out presentation-attack evaluation |
| Lookalike or threshold error | Calibrated verifier, multiple samples, one-to-one confirmation where approved, conservative threshold | FAR/FRR report on representative consented set |
| Multi-person substitution | Exactly one face through capture; bind same tracked face through attempt; cancel on track change | Multi-person and swap tests |
| Tailgating | Door-side physical controls/signage/staff process; optional non-identifying occupancy sensor | Pilot observations and incident review |
| Booking replay or stale membership | Authoritative current lookup with local timezone, bounded cache, member active/consent/revocation checks | Midnight/DST/stale-cache tests |
| Missing/stale health | Required component set, freshness TTL, exact `HEALTHY` rule | Empty/missing/degraded/error/stale health matrix |
| Dependency timeout/MITM | HTTPS, certificate validation, deadlines, scoped rotating credentials, fail closed | Timeout/TLS tests and deployment scan |
| Crash before/after unlock | Durable fail-closed attempt before call, idempotency key, terminal reconciliation | Kill-point and duplicate-delivery tests |
| Duplicate wake/command | Single-flight session plus durable attempt/session IDs and intent deduplication | Concurrent wake and duplicate-intent tests |
| Compromised local storage | Disk/DB encryption, OS service account, secret store, encrypted embeddings/tokens, retention deletion | Host hardening and restore/deletion tests |
| Microphone/camera privacy breach | Local idle wake; explicit attended launch/Start chat or opt-in dashboard opening; visible microphone state and Stop; on-request snapshots and minimum retention | Network-byte idle test, Stop/reopen checks and data inventory |
| Robot movement injury/damage | Bounded motion arbiter, low-torque idle, cancellation, keep-clear placement | Motion contention and emergency-stop tests |

## Fail-safe response

On ambiguity the door stays locked and Reachy gives a non-sensitive staff/manual fallback. Access and security incidents disable physical actuation independently of conversation. Logs must not contain raw face frames, embeddings, audio, access tokens, or API keys.

## Open owner approvals

Named owners, retention periods, the non-biometric fallback, tailgating response, after-hours policy, and incident escalation contacts must be recorded before a live pilot.
