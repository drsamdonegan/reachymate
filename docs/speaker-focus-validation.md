# Speaker focus validation

Gate D requires an engaged face, speech and agreeing valid DoA; unavailable or unvalidated DoA uses face-plus-speech and reports degraded focus. The default is closed. Gated PCM, including digital silence, is used both for Realtime and local command recognition. Wake detection remains local and does not require a face.

Gate P also requires measured mouth activity. `MouthActivity` loads a local MediaPipe Face Landmarker asset, computes mouth aspect ratio on the engaged face only, and stores a 400 ms scalar window. It produces no embedding and sends no frame externally. Missing landmarks close Gate P. A still front-facing mouth vetoes broadside speech; two measured behind-source dither verdicts latch a five-second veto. The physical dither survey is pending: the runtime reports degraded focus until that experiment has been validated. Automatic unattended dither motion is not enabled by a utility test.

`app/scripts/evaluate_av_gate.py` replays labelled feature logs without audio or images:

```sh
.venv/bin/python -m app.scripts.evaluate_av_gate \
  data/audio-corpus/features.json --mode pilot \
  --output docs/evidence/phase6/av-gate.json
```

The JSON input contains a `frames` array. Each frame has `session` (anonymous session label), `noise_dba`, `time_s`, `label` (`visitor`, `bystander`, `rear`, or `silence`), and `vad`. Optional `face` fields are `track`, `bearing_deg`, `age_s`, `in_zone`, `mouth_active`, `mouth_still_s`; optional `doa` fields are `theta` in radians, `speech`, `age_s`. `bystander_barge_in` records an observed interruption. No name, voiceprint, frame, or embedding belongs in this log.

The evaluator fails missing evidence. Pilot validation needs at least 20 sessions at each of 55, 65 and 72 dBA, bystander false-open ≤5%, visitor clipped ≤3%, rear speech admitted ≤2%, and at most one bystander interruption per 20 sessions. Feature replay estimates these measures; an attended doorway run must verify utterance-level rear turns and that pre-roll preserves the starts of visitor speech.

For an explicitly consented audio corpus, `record_consented_session.py` records the same mono post-DSP PCM read by `AudioRouter`, after channel selection and WebRTC AEC/NS. It requires `--consent-confirmed --notice-displayed`, an output below ignored `data/audio-corpus`, and a duration of at most five minutes. All audible participants must have consented. Files are created with mode 0600 and cannot overwrite existing recordings. Consent metadata records a 30-day retention period; the operator is responsible for that corpus lifecycle. These recordings are separate from the feature-only probes.

Optional GTCRN (`GTCRN_MODEL_PATH`) runs locally after WebRTC AEC/NS, outside the PortAudio callback. Missing assets fail configuration; startup outputs silence while the streaming model warms up. The asset is not bundled and the feature is disabled by default.

Optional A/B controls are disabled by default: 700 ms zero-tail then no append, local barge-in after 160 ms of gated speech, fixed beams following a measured face bearing at no more than 2 Hz, and Vosk confirmation after a sherpa hit. Local barge-in must remain disabled during the biometric notice. Fixed beam steering requires evidence of improvement over factory auto-selection. These controls require the actual doorway corpus before selection for the pilot.
