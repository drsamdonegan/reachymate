# Architecture

## Main Flow

1. The audio loop runs continuously in low power mode.
2. Silero VAD gates wakeword scoring.
3. When the wake detector crosses threshold and a face is nearby, the state machine enters `ENGAGED`.
4. The vision pipeline samples a short burst of frames, rejects ambiguous or poor-quality inputs, generates an embedding, and matches it against the local face store.
5. If a user match is found, the booking client queries `mlai-backend` for coworking bookings and treats `booked` for `today` as active in v1.
6. If policy allows, the unlock service obtains a SALTO token and sends a single unlock request guarded by per-event idempotency.
7. Every decision is logged locally with a reason code.

## Boundaries

- Reachy hardware, camera, and microphone are adapters.
- `mlai-backend` is the booking and Slack owner.
- SALTO is isolated behind OAuth and lock adapters.
- SQLite or Postgres handle local persistence.
- The browser UI only talks to the local FastAPI app.

## Local Data

- `users`
- `face_embeddings`
- `access_events`
- `system_health`
- `salto_tokens`

Raw face crops are not retained by default.
