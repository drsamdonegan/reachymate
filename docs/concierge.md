# Concierge implementation and release gates

The runtime evolves the existing FastAPI application. A private pre-change snapshot protects the working tree; no research notes or deployment evidence are required in the public source distribution.

## Behavior

- Dormant: camera closed, local keyword/VAD/DoA processing; the power manager parks and disables torque.
- Engaged: single-writer motion, detection-only tracking, Gate D or opt-in Gate P, 24 kHz Realtime transport. The API receives silence when the gate closes.
- Consent: fixed pinned local clip, fresh gated local confirmation within eight seconds. Wake, model inference and a bare yes never authorize capture. Routed sessions remain non-biometric.
- Access: scoped, multi-frame local recognition over recorded opt-in members, encrypted row-bound templates and deterministic booking policy. Shadow returns DENY/SHADOW_WOULD_ALLOW and one non-actuating row. Default speech reveals no membership or booking details.
- Routing: server-side device/session/track context, fixed backend origin, typed intents, rate limits, durable idempotency and outbound host-status polling. No names are stored in visitor route records.
- Recovery: measured task heartbeats, bounded restart/reboot attempts, ACCESS_DISABLED for backend/storage/key faults and SAFE_HOLD for hardware faults.

## Remaining release gates

Software tests do not certify wake accuracy, source direction, biometric accuracy, PAD resistance, door opening or unattended recovery. Required field work includes calibrated intrinsics, L45/R45 surveys, motor/sag verification, consented acoustic/recognition/PAD held-out sets, signed kiosk commissioning, power/USB drills, 72-hour soak and a monitored pilot.

The backend owner must deploy the front-desk/member directory and device-bound KEK endpoints. The recommended owner is the existing bookings/Slack backend maintainer, with credential custody assigned to the infrastructure/security maintainer.

Physical door preparation is isolated in `app/services/door/`: stages, signed approvals, sensor-bound confirmation, uncertainty reconciliation, durable reservations and a kill switch. The application composition remains frozen at D0; flags cannot enable a live door.

## Data and operations

Member withdrawal/deletion removes templates and recorded consent immediately. Standalone enrollment, export, rewrap and calibration tools can fetch a remote KEK without starting the robot or cloud conversation. No raw images or ambient audio are saved during ordinary operation. Consented recordings require explicit command-line flags and remain in an ignored local directory.

`deploy/macos/` contains the signed-wrapper source, plist generator, installer, watchdog, sibling caffeinate and power settings. These are commissioning tools; build or installation alone does not establish TCC persistence or power-loss recovery. Use a dedicated kiosk and stable signing identity.
