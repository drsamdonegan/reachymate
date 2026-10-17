# Architecture

The canonical runtime is `app/`. FastAPI serves the existing local dashboard and diagnostics; a single media adapter owns Reachy’s USB microphone, speaker and camera, and one motion thread owns SDK targets.

## Conversation

1. At rest, local wake detection listens while the camera is off and the robot is parked with torque off.
2. A local wake starts a bounded GPT-Live session. The ordinary concierge uses a detection-only face tracker and audio-visual gate to select speech.
3. The staging attended mode starts through the dashboard or launcher. It admits continuous echo-cancelled microphone audio during head movement and face loss, until Stop, cancellation or ten minutes.
4. GPT-Live `gpt-live-1` listens and speaks concurrently over the native Live WebSocket at 16 kHz. Its `gpt-6-luna` Responses backend handles constrained tools. Realtime remains a separate explicitly selected transport.
5. Seven concierge tools cover approved public knowledge, local access requests and visitor routing. A bounded movement tool adds look, pose, gesture and recorded dance/expression requests. The opt-in attended vision tool sends fresh Reachy camera snapshots to the Responses backend on visual requests.
6. Local consent notices suspend cloud input. The model cannot establish identity, grant consent or control a door.

## Access and routing

An access request passes through deterministic consent, membership/revocation, presentation-attack detection, face, booking and health checks. Durable event and command records prevent duplicate actions. The active shadow profile records what a placeholder would have done; it never composes a physical actuator. Image quality is not presentation-attack detection.

Front-desk requests use typed, deduplicated contracts. The local launcher runs loopback booking and front-desk mocks: their replies do not notify staff. Real service ownership and live notification acceptance remain external gates.

Approved factual answers require the public-only knowledge service. Private Roo/member sources remain outside the lobby model’s tools. Tool results, retrieved content and camera pixels are untrusted data.

## Launch and persistence

`Start Reachy.command` serializes startup, validates existing service identities, starts missing services, waits for readiness and connects attended voice. It reuses healthy processes rather than creating a second hardware/media owner. The dashboard auto-start switch is restricted to the staging attended profile. Login startup is a separate kiosk deployment workflow.

SQLite or Postgres stores users, encrypted face templates, consent and access events, unlock command records and health. Legacy SALTO tables remain for migration compatibility. Raw face crops, camera snapshots and room audio are not retained by default. API keys, databases, local models, logs and site-specific evidence stay outside Git.

Missing PAD and measured calibration keep biometric/access acceptance incomplete independently of attended voice. See [the test guide](testing.md) for the remaining physical and backend gates.
