# Shorts recovery after the timing and visual fixes

These recovery commands were executed against the listed live project versions on 2026-09-28.
**Do not repeat them without checking current state and version first.** The Telegram worker
continues each project and may incur OpenAI TTS/image requests after the local recovery step.

The Telegram worker was restarted with the new Python code. `Start Telegram Worker.exe` does not
replace an already-running worker; do not launch a duplicate.

## Recovery order

1. Script-lock numeric serialization only (approved narration and recorded voices are unchanged):
   `studio --root projects/live retry <id> --version <version>` for `d480da08c77f` v3,
   `0ea66ce6b2fe` v3, and `bca94d8663f5` v3. The verified lock comparison now accepts the
   legacy `60`/`60.0` difference while still rejecting changed script content.
2. Narration reviews that now fit the 90-second automatic window:
   `studio --root projects/live recover-timing-layout <id> <version>` for
   `373bf3269609` v3, `aee59b9e3891` v8, and `ac335c1cbd70` v3. This supersedes the
   obsolete non-approvable review; the next worker pass reuses measured voice recordings and
   reflows shots. It does not silently approve a script.
3. Planet Nine asset plan `3dd23e2153da` v4: `recover-assets` can revalidate the cached
   `34205a28b656eac328ce942f8e7873b954d81e72dcc45a2d3d3929253eca6d5b.json`
   response in that project's `provider-cache`, with no planning call. The full-width two-item
   chart now fits its approved scientific labels. Subsequent image generation may still cost.
4. `9aa5f80ef604` v1: `recover-director` can replay the cached narrative and the first
   shot-plan response, `8bbe59d4328d30d0782f8ec71cef5e5cba58c1b6a79c295c0347f8a4e5a9a63f.json`,
   in its `provider-cache`. The compound metric becomes a two-slot metric pair, and the shot
   schedule is normalized. No new director request is made; TTS has not yet been prepared.
5. For `d5e760ea2881` v3, `reframe-semantic` creates a render-only version. It replaces
   false equal bars and the one-year pseudo-timeline, while reusing the existing relevant
   cave/study illustrations, narration, and music. A new prototype approval is required.
   Existing recorded voice tone is unchanged in this no-API reframe. Newly synthesized Shorts
   use one stable delivery instruction across shots; a revoice of this particular project
   would require new paid TTS and a separate timing/preview review.

## Policy and safeguards

- Full Shorts target about 60 seconds. The measured timeline may automatically grow to 90
  seconds without trimming speech; 90–180 seconds is offered as an explicit keep-or-revise
  choice. The 10–15-second excerpt prototype remains unchanged.
- Source claims, approval decisions, and old artifacts remain versioned. Recovery commands
  reject stale project versions and do not call external providers themselves.
- For a validated MP4 larger than Telegram's upload threshold, the worker copies the unchanged
  file to the project root and sends its local path rather than recompressing it.
- Usage reports distinguish current-version usage from lifetime text-model requests, including
  cached failed validation responses. TTS and image costs remain unknown where the provider
  supplied no charge receipt.

## Publication recovery for `d5e760ea2881`

The v4 render-only visual refresh had no current-version YouTube metadata. On 2026-09-28,
`recover-publication d5e760ea2881 4` rebuilt the metadata and evidence manifests from saved
project and music-selection artifacts without provider calls. Publication clearance then reached
the v4 approval gate; it was approved and completed, with the final MP4 and metadata in the
project root. The final MP4 hash matches the approved preview. Future render-only refreshes
exclude stale evidence manifests, and publication clearance rebuilds missing current-version
packages locally. The already-running worker retains its loaded code until it is restarted;
do not interrupt an in-flight image or render job solely to reload this safeguard.

## Map-shot asset-plan recovery for `aee59b9e3891`

Both saved v9 planner responses assigned an archival image to `shot_02` while selecting the
procedural `map` strategy. That image would not be used by the renderer. The generic asset-plan
normalizer now removes map-shot image assignments while preserving images shared with other
shots, and the planner prompt explicitly disallows image briefs for procedural maps. Both cached
plans passed validation without new model calls. On 2026-09-28, the richer first response,
`362b3f66814a68d21486878edea4b0e0766aafe216a762f14830d263b76bcee6.json`, was
recovered into v9 with seven remaining image briefs; the worker can continue asset preparation.
