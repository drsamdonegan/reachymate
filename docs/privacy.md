# Privacy

## Biometrics

- Voice is not an authentication factor.
- Face recognition is based on stored embeddings, not raw face crops.
- Enrollment should only occur with explicit user consent.
- Signage alone is not treated as biometric consent.
- Raw enrollment images should be treated as transient input and not retained unless a separate audit policy explicitly requires them.

## Operational Boundaries

- fail closed on ambiguous face input
- fail closed when booking lookup fails
- fail closed when the entry-action boundary fails
- never unlock on face match alone
- treat passive image quality as quality only, never liveness/PAD
- send room audio only after a local wake or an explicit attended launch/Start chat
- the opt-in launcher enables chat on dashboard opening; closing a tab does not stop chat, and Stop chat does
- send camera snapshots to OpenAI only on visual requests during attended chat; do not archive images or stream continuous video
- never expose private Roo/member sources to the lobby model

## Recommended Controls

- publish a biometric consent notice near the device
- provide an opt-out path and manual fallback process
- restrict enrollment and audit-log access to administrators
- rotate OpenAI and MLAI API credentials
- store `DATA_ENCRYPTION_KEY` outside the database and back up/rotate it under a documented key process
- collect doorway audio only in a consented, purpose-labelled evaluation session
- complete a privacy impact assessment, retention/deletion/export process, and non-biometric fallback before a biometric pilot

Site-specific plans, security research, audits and measured evidence stay private and are excluded from this public repository. Software tests and the general operating instructions do not establish a real-site biometric or access deployment approval.
