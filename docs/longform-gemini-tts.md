# Shared Gemini narration

New Shorts and long-form projects default to Gemini 3.8 Flash TTS with the Aoede voice. Already-created projects retain their saved voice provider, model, and narrator. The approved narration text is sent verbatim; delivery direction is supplied as Gemini speech metadata. Existing timing and retiming stages still run after synthesis. Shorts entrance cues continue to use estimated word positions, not Whisper alignment.

Put `GEMINI_API_KEY=...` in the repository's ignored `.env` file. Do not put a key in a project brief or Telegram message. The worker must be restarted to load the new code and environment. The existing OpenAI key remains necessary for the other live pipeline stages and word alignment.

Telegram commands:

- `/ttsprovider` — show the future-project provider for both formats.
- `/ttsprovider gemini` — persist Gemini/Aoede for future projects.
- `/ttsprovider openai` — persist the configured OpenAI voice for future projects.
- `/ttsprovider reset` — return to the configured default (Gemini unless overridden).
- `/settings` — show the saved provider alongside other future-project settings.

The setting is snapshotted when `/full`, `/topic` or `/longform` creates a project; it never revoices an approved script or completed video. `/longformtts` remains a compatibility alias, and a previously saved preference is honored. Reset clears that older preference too. `STUDIO_TTS_PROVIDER` sets the shared environment default; the older `STUDIO_LONGFORM_TTS_PROVIDER` is accepted as an environment fallback. The existing `STUDIO_LONGFORM_TTS_MODEL` and `STUDIO_LONGFORM_TTS_VOICE` configure Gemini for both formats; OpenAI keeps `STUDIO_TTS_MODEL` and `STUDIO_VOICE`.

Gemini audio is cached under each project, keyed by approved text, direction, model, voice, and media epoch. New Shorts signatures track the selected speech model only; existing schema-2/legacy signatures remain unchanged. Long-form uncertain TTS requests retain their reservation for manual inspection rather than triggering an unchanged retry.

For the Marie Curie Chapter 1 comparison, run `python scripts/compare_longform_gemini_tts.py 22555d2d95c6 --root projects/live`. This creates `comparisons/gemini-aoede-ch1/original-chapter-1.wav` and `aoede-chapter-1.wav` from the same approved four blocks without changing the saved project or video. The generated block WAVs are cached so reassembling the comparison does not issue more Gemini requests. This is an audio-only voice comparison, not a new approved cut or a transcription-verification result.
