# Privacy

## Biometrics

- Voice is not an authentication factor.
- Face recognition is based on stored embeddings, not raw face crops.
- Enrollment should only occur with explicit user consent.
- Raw enrollment images should be treated as transient input and not retained unless a separate audit policy explicitly requires them.

## Operational Boundaries

- fail closed on ambiguous face input
- fail closed when booking lookup fails
- fail closed when SALTO token or unlock actions fail
- never unlock on face match alone

## Recommended Controls

- publish a biometric consent notice near the device
- provide an opt-out path and manual fallback process
- restrict enrollment and audit-log access to administrators
- rotate API keys and SALTO credentials
