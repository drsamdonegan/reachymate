# Standalone hardware probes

Run these from the Terminal process that owns Camera and Microphone permission, with the app stopped. They do not initialise the application or database. Use the pinned `.venv`; JSON reports go to `docs/evidence/phase0/`. A 5 GiB disk floor is enforced. Raw audio and images are not written by these probes.

| Command | Evidence |
|---|---|
| `.venv/bin/python -m app.scripts.hw.cpu_probe` | Offline model timings on synthetic input; inference only, no hardware or recorded face |
| `.venv/bin/python -m app.scripts.hw.tcc_probe` | Camera/mic authorization, physical camera name/uniqueID, installed versions |
| `.venv/bin/python -m app.scripts.hw.xvf_probe` | Volatile chip registers, firmware, 50 DoA reads and latency |
| `.venv/bin/python -m app.scripts.hw.xvf_probe --concurrent-audio` | DoA reads while 16 kHz stereo audio is open; capture must deliver at least 90% of expected frames |
| `.venv/bin/python -m app.scripts.hw.audio_probe` | Ten seconds of per-channel RMS, correlation, zero runs and stream flags; incomplete capture fails |
| `.venv/bin/python -m app.scripts.hw.audio_probe --play-tone` | Same, with a one-second chirp at −20 dBFS; attended audio output |
| `.venv/bin/python -m app.scripts.hw.camera_probe` | Physical Reachy mapping, 1080p MJPG negotiation, warm-up and FPS |
| `.venv/bin/python -m app.scripts.hw.camera_probe --checkerboard 9x6 --seconds 30` | Checkerboard calibration in RAM; move the board through the view. Requires at least ten usable views. No board or face frames are saved |
| `.venv/bin/python -m app.scripts.hw.doa_survey --bearing-degrees 45 --label left45 --pose awake` | Human-labelled angle/error/speech fraction; repeat for ten utterances at each required position |

Two probes change hardware state and must be attended:

- `xvf_probe --concurrent-audio --reboot` reboots the chip while CoreAudio holds it. **A reopened control handle is insufficient:** recovery requires fresh USB discovery, the CoreAudio device, and a fresh one-second nonzero stream with at least 90% expected frames and no status flags. USB disappearance is reported as `usb_or_coreaudio_vanished_replug_required`; a replug is then required.
- `motion_probe --execute` requires a clear robot and a loopback daemon. It wakes, turns ±20°, tests `StopMoveCmd`, and verifies sleep/torque-off and sag. It is opt-in; do not run it unattended. The daemon must use `--no-media --no-wake-up-on-start --goto-sleep-on-stop`.

Factory is the default XVF profile. Alternative profiles apply verified volatile writes. No code calls `SAVE_CONFIGURATION`.

Camera calibration output stays `validated: false` until the operator performs the tape test at 0°, ±15°, ±30° and ±35° and proves the plan's ±5° error bound at ±30°. Keep the measured `K`, `D`, `width`, and `height`; record the tape evidence before marking that local calibration validated. Normal profiles refuse to derive an engaged track from missing or unvalidated calibration. The explicit staging shadow `--demo` profile can use SDK intrinsics for detection-only conversation, marked degraded and without face-directed motion; it cannot validate calibration, DoA accuracy or biometric readiness. A matching calibration derives `W_zone = fx × 0.15 / 1.5` and `W_crop = fx × 0.15 / 1.1`.

DoA mechanics and directional accuracy are distinct. Passing the concurrent mechanics probe does not set `DOA_BEARING_VALIDATED`. The labelled attended survey remains necessary.
