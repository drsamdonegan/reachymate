# Automatic event welcome

The event greeter uses Reachy's own camera to detect nearby faces, turn toward a visitor and deliver
an operator-approved welcome through GPT-Live. Detection stays local: it does not match identities,
create face embeddings, save images, check tickets or control access.

## Run

1. Connect USB and external power, then open **Start Reachy.command** or the existing dashboard.
2. If Luma was in use, close its scanner tabs and return the camera first. Stop browser camera sharing.
3. In **Automatic welcome**, enter the arrival welcome and click **Start event greeter**.
4. Stand approximately a metre in front of Reachy. A stable face triggers one welcome. He follows
   its position with small head corrections, while explicit dances/poses take priority.

When no nearby face is detected, Reachy gently searches left and right. A welcome re-arms after
five seconds without a visible nearby face, with at least fifteen seconds between welcomes. Brief
occlusion, tracker-ID changes and leaving the preferred tracking zone do not themselves trigger
another welcome. This is presence-based, not person identification: a returning visitor can be
welcomed again, and a group staying together is treated as one arrival.

**Stop movement** holds the head still, including automatic searching, tracking and talking motion.
A new movement or chat re-enables movement. **Stop event greeter** (or **Stop chat**) stops the greeter,
cloud voice and face watching. Closing the dashboard leaves the greeter running.

Each cloud conversation retains its existing time limit. Local face watching continues afterward;
the next arrival starts a new bounded voice session. Say **Hey Reachy** or start chat to resume a
conversation without leaving. Camera questions such as **What am I holding?** use the existing
on-request vision path; automatic welcomes do not upload face images to the model.

## Startup preference

The staging demo profile is required. Set these in the private `.env` to make the launcher or a newly
opened dashboard start the greeter instead of ordinary chat:

```dotenv
DASHBOARD_AUTO_GREETER=true
EVENT_GREETER_GREETING="Welcome to the hackathon! I'm your AI robot host."
```

Restart the app after changing `.env`. Reopening the launcher reuses an active greeter without a
second microphone/camera owner. Stop stays effective across dashboard polling; a new dashboard opening
or launcher invocation follows the configured startup preference. Keep event-specific settings private.

## Movement and verification

The greeter uses bounded image-position feedback rather than requiring calibrated world coordinates.
It searches within ±25° yaw, follows within ±35° yaw and ±18° pitch, and limits corrections per update.
The existing single motion thread still applies pose, slew, inverse-kinematics and joint limits. It
rejects automatic updates during explicit movements, Stop, capture freeze or parking. The calibrated
attention callback remains separate; SDK-default intrinsics are not represented as measured evidence.

Test a welcome, continuous following, a brief occlusion, leaving for five seconds and returning after
the cooldown. Verify no repeated welcome while the face stays visible. Test Stop movement, then a new
look request; test Stop event greeter and reopening the launcher. A hardware fault stops face watching,
cloud voice and motion. Check power/USB before restarting after a motor communication error.

The dashboard shows detection state and welcome count. Tests cover arrival hysteresis, motion bounds,
Stop priority, asynchronous greetings, camera ownership, voice expiry/restart and launcher reuse.
