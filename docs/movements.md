# Reachy movement library

Reachy uses one motion controller for head position/rotation, body yaw, both antennas, looks, gestures,
dances and recorded expressions. GPT-Live can request every supported action in ordinary chat and the
Luma event-host mode. Moving works without vision; **Enable camera sharing** adds requested camera
stills through Chrome while Luma scans, without reopening the native camera.
Let the head settle before showing a ticket to the camera.

## Install or refresh the Hugging Face catalogue

```sh
uv run python -m app.scripts.provision_movements --community
```

The command installs the pinned official Pollen dance and emotion libraries, then discovers every public
dataset tagged `reachy_mini_community_moves`. This is the tag used by [Pollen's movement gallery](https://pollen-robotics.com/reachy-mini/moves/).
The [Reachy SDK's recorded-move format](https://huggingface.co/docs/reachy_mini/en/examples/recorded_moves)
is used for playback. Only motion JSON is imported; dataset scripts and audio are never executed or played.
The importer respects Hugging Face's retry windows when rate limited. Rerun it after download failures;
pinned files already in the local cache are reused.

Click **Reload installed moves** after provisioning. This swaps the catalogue without interrupting voice,
an active movement or Luma's camera. Restarting the application also loads it. Reopening an already-running
launcher reuses the current process. An app restart preserves Luma's camera handoff and leaves voice off;
prepare the handoff and explicitly start the host again from the dashboard.

The local `data/movements/community-manifest.json` records each source repository, exact revision,
original filename, declared licence, checksum, and any unsupported files or download errors. Source
licences remain those of their publishers. A missing licence is recorded as unspecified. Data and
manifests stay outside Git. Files are checked again when selected and loaded on demand to keep memory
usage bounded. Motion metadata is treated as untrusted content, never as instructions to the model.

## Use the moves

- Say “look around”, “do a dance”, “nod”, “wiggle your antennas”, or request a named Hugging Face move.
- The model uses `robot_motion` with `action: list`, a `query`, and an optional `offset` to search
  pages of up to twenty moves. Results include the exact action and name needed for playback.
- The dashboard's **Movement library** offers official dances, expressions and community recordings.
  **Find a movement** searches names and creators. Broad results show the first 200 matches; narrow
  the search to find any of the remaining moves.
- Talking animation follows the played audio while no explicit movement is active. After an expression
  finishes, it continues around the held pose. Explicit dances take precedence over speech animation.
- **Stop movement** cancels the trajectory and pauses talking/breathing motion. Another movement or
  a new chat re-enables it. Stop host voice or the session time limit also ends animated conversation.

The existing controller bounds duration, speed, pose and joint travel for every source. Recorded moves
are reduced in amplitude when necessary for Reachy's reachable workspace. A move that still fails
validation is reported unavailable and never sent to the motors. Long recordings play a bounded excerpt
(20 seconds maximum plus transitions), and new requests replace the current movement.

## Verify

Ask Reachy to greet you and watch his head move with speech. Request a look-around, a short dance and a
named community recording, then ask him to speak again. Verify Stop holds him still and a new look request
restores movement. In event mode, Luma should remain in Express Mode with a live camera preview throughout;
model image count stays at zero unless camera sharing is enabled and a visual question is asked.
Automated tests cover held-pose speech, Stop/freeze priority, host tool
access, catalogue pagination, checksums, and bounds without running physical movements.
