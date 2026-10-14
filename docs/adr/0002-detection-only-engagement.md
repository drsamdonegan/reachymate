# ADR 0002: detection-only engagement

Camera capture is closed in dormant/idle states and enabled only for an explicit engagement or operator diagnostic. Tracking uses local face locations, calibrated bearings and optionally lip movement. Frames stay in process memory.

Tracking cannot call the embedder. Access and enrollment use an explicit guarded scope; a member request first plays the pinned local notice, and only fresh gated local “check me in” authorizes the access scope. Routed visitors are excluded.

Calibration and privacy commissioning are external release gates. Detection-only tracking does not establish identity, consent for recognition, or PAD.
