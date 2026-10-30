# Doorway product policy matrix

This is the implementation baseline. Rows marked **owner approval required** keep physical actuation disabled until approved.

| Situation | System action | Door action |
|---|---|---|
| Idle | Local wake detector and supervisors only; parked pose, torque and camera off | None |
| Attended demo launched or auto-chat dashboard opened | Bounded continuous GPT-Live conversation, disclosed microphone streaming; visual snapshots on request | None |
| Wake detected, no usable face | Chat may continue; offer staff/manual fallback | Deny |
| More than one face or face track changes | Ask people to separate and restart | Deny |
| Unknown member | Do not disclose membership details; offer staff fallback | Deny |
| Inactive, revoked, or no recorded biometric consent | Explain that face access is unavailable; offer non-biometric fallback | Deny |
| Verified active member, no current booking | Explain booking requirement and offer public booking help | Deny |
| Booking/member provider fails, times out, or returns malformed data | Give a temporary-unavailable response | Deny |
| Any required health missing, stale, degraded, or errored | Chat may degrade independently | Deny |
| Verified eligible member with current booking and all safeguards healthy | Create durable attempt, then record a shadow placeholder result | No physical action in the current runtime |
| SALTO rejects/times out/throws/returns uncertainty | Record terminal denial and send member to staff | Deny |
| Voice says “open the door” or model requests access | Emit a deduplicated access request only | None directly |
| Guest or delivery | **Owner approval required:** default staff escalation | Deny by default |
| After hours | **Owner approval required:** default deny | Deny by default |
| Manual override | **Owner approval required:** physical staff-controlled mechanism, audited separately | Never model-controlled |

## Initial interaction choices

- Wake phrase: **“Hey Reachy”** (to be validated in the room).
- Local face checks require the fixed notice followed by the person's own “check me in”; model requests cannot supply consent.
- Member selector: face verification against opted-in active members only after that request; no passive greet-by-name identification.
- Fallback: direct the person to coworking-space staff; no self-service guest unlock.
- Cooldown: four seconds after denial; repeated attempts are rate-limited without revealing identity confidence.

## Required names and external decisions

Before physical pilot, record: Reachy edition/host, coworking owner, access/security owner, privacy/consent owner, incident owner, MLAI public-corpus owner, member/booking-service owner, SALTO product/integration/site/lock and certified installer, retention periods, guest/after-hours/tailgating rules, and the manual fallback procedure.
