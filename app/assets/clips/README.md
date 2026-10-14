Fixed offline clips for the concierge, rendered locally from the reviewed text with macOS Samantha on 13 October 2026. No cloud TTS or microphone capture was used. They are mono PCM16 WAV at 24 kHz; the application pins their SHA-256 digests in `fallback_audio.py` and refuses altered files.

- `consent_notice.wav`: "I can check you in by comparing your face with members who've opted in; nothing is kept if you're not a match. Say 'check me in' to go ahead, or 'no thanks' for the front desk."
- `cloud_unavailable.wav`: "The online conversation is unavailable. Say 'open the door' for a local check-in notice, or ask the front desk for help."
- `frontdesk.wav`: "Please see the front desk for help. I haven't opened the door or notified anyone."
- `goodbye.wav`: "Goodbye. Say 'Hey Reachy' if you need a hand."

Consent begins only after `FallbackAudio.play('consent_notice')` completes and the local grammar is reset. Production deployment still requires an owner review of the notice and signage.
