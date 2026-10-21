# Shorts card layout, study cues and shared TTS

Implemented for future projects; completed project `99a941f3caca` remains unchanged.

1. Qualitative-comparison card labels are vertically centered when they fit on one line. Wrapped labels remain top-aligned. Font fitting, safe-area checks, contrast checking and conditional local backings are retained.
2. The Shorts director requests one exact spoken phrase per animated `study_design` step or `qualitative_comparison` alternative. Other shot types retain their existing optional-cue behavior. Timing still uses estimated word positions within the measured recording, not ASR/Whisper. Old saved plans without cues retain fixed-schedule fallback. Existing validators check supplied cue counts and spoken order; no new blanket cue gate was added.
3. `/ttsprovider gemini|openai|reset` governs future Shorts and long-form projects. Gemini 3.8/Aoede is the default. Saved projects and prepared recordings are not switched or invalidated by changing the preference.

Read-only proofs with original audio:

- `projects/live/99a941f3caca/comparisons/card-alignment-and-cues/shot-4-before.mp4`
- `projects/live/99a941f3caca/comparisons/card-alignment-and-cues/shot-4-after.mp4`
- `projects/live/99a941f3caca/comparisons/card-alignment-and-cues/shot-5-before.mp4`
- `projects/live/99a941f3caca/comparisons/card-alignment-and-cues/shot-5-after.mp4`

The cue proof supplies three exact anchors on an in-memory copy only. It does not replan, approve, or save a revision of the original project, and makes no provider requests.
