# Deterministic speech context for image planning

The visual planner chooses the image subject, moment, composition, framing, and overlay-safe region. Studio attaches the approved narration using each beat's existing `spoken_unit_ids`. The model-facing response schema omits `ImageDirection.spoken_idea`; the persisted domain field retains the exact script text with no display-oriented character limit.

This addresses project `97440dde8519`'s Chapter 1 failure: Luna returned a corrupted 180-character copy of a 185-character excerpt. The image concept fit the speech, but the exact-excerpt check rejected the altered ending. The corrected assembly derives the text before domain validation for both generated requests and authentic-image fallbacks. Existing saved response excerpts are replaced in memory by their referenced approved speech; the original provider response is preserved for diagnostics. Overlay labels and their timing anchors retain their existing contracts.

Recovery revalidates the already-paid chapter responses and allows live continuation only for unfinished chapters. It resets the failed visual-planning attempt, preserves the project version and human decisions, and still stops at visual-plan approval. Other validation failures continue to halt the project under its saved zero-repair policy.

Validation: 167 focused tests passed, covering image context binding, authentic and generated image requests, provider schemas, invalid unit references, recovery, existing layout/reveal behavior, and image presentation. Saved Chapter 1 responses also passed partition, unit coverage, image direction, and design-family validation without a new model request.
