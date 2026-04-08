# Troubleshooting

## App Starts But `/healthz/ready` Is False

- inspect component status in the admin UI
- confirm database permissions
- confirm `MLAI_BACKEND_URL` and `MLAI_BACKEND_API_KEY`
- confirm SALTO environment variables if running against the real vendor API

## Camera Snapshot Returns `503`

- in simulation, enable `SIMULATION_USE_WEBCAM=true` if you want a real webcam
- on hardware, confirm the Reachy SDK can read frames
- run:

```bash
python -m app.scripts.diagnose_camera
```

## Wakeword Never Triggers

- confirm audio dependencies are installed
- confirm microphone access
- lower `WAKEWORD_THRESHOLD`
- inspect live scores with:

```bash
python -m app.scripts.diagnose_audio
```

## Face Match Is Too Strict Or Too Loose

- collect more enrollment samples
- recalibrate with:

```bash
python -m app.scripts.calibrate_face_threshold
```
