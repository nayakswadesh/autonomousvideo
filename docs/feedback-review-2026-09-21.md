# Autonomous Studio — Feedback and Bug Review

Date: 2026-09-21  
Status: reviewed; accepted items are implemented in v0.9

## Executive assessment

The accepted product suggestions are directionally sound:

1. The visual pipeline should be able to use licensed real photographs and archival images, not only generated illustrations.
2. Full productions should have a script-lock approval after measured narration passes and before image generation begins.

The reported technical issues also expose four genuine framework problems:

1. caption preferences are insufficiently auditable and the implemented command name differs from the originally requested spelling;
2. the display planner and renderer can repeat the same metric in two visual slots;
3. semantically invalid model outputs are cached and then reused by `/retry`, making some failures deterministic; and
4. incomplete provider responses lose their diagnostic details and are escalated to the user too early.

The resulting v0.9 scope contains reliability and script locking first, then archival imagery.
Model visibility and Telegram model/voice configuration were subsequently removed from v0.9 by
product decision and are not implemented.

## 1. Real photographs, people, places, and archival material

### Assessment

Yes. The studio should be able to use real topic-related images and then crop, grade, composite, annotate, or otherwise adapt them when the specific license permits derivatives.

The current `OpenAIImages` provider always creates a new illustration. The `AssetPlan` can choose a generated image, map, chart, or text treatment, but it cannot search for or select a real source image. That is a product limitation, not a limitation of the declarative renderer.

### Recommended visual-source policy

For each shot, the planner should choose among:

1. `archival_image` — a real photograph, portrait, place, object, document, or historical image;
2. `archival_image_with_overlay` — a real image with renderer-owned titles, metrics, maps, timelines, or charts;
3. `generated_image` — an original illustration when real material is unavailable, unsafe, misleading, or not licensable;
4. `map`, `chart`, or another code-rendered explanatory graphic; and
5. `text_only_intentional` — still exceptional, with an explicit rationale.

The planner should prefer real imagery when it materially improves recognition or credibility: named people, identifiable places, historical events, buildings, artifacts, geographic features, and documented objects. It should not force a real photograph into an abstract explanatory scene where a chart or illustration communicates better.

### Required provider boundary

Add a `MediaSearchProvider` and a `MediaAcquisitionProvider`, separate from image generation. Initial adapters should target repositories with usable rights metadata, such as Wikimedia Commons and clearly marked Library of Congress “Free to Use and Reuse” collections.

Search results must not be treated as approved assets. Acquisition should validate and store:

- original file;
- canonical source page and direct file URL;
- creator/photographer;
- source institution;
- license identifier and license URL;
- attribution text;
- whether commercial reuse is permitted;
- whether derivatives are permitted;
- share-alike obligations;
- known personality/privacy/trademark warnings;
- retrieval timestamp;
- source and local checksums; and
- any generated derivative as a separate artifact linked to the original.

Wikimedia Commons allows broad reuse but each file can have different attribution, license, derivative, and share-alike requirements, and Commons itself advises reusers to verify each file’s status. Identifiable people may also raise personality or privacy rights independent of copyright. See [Wikimedia Commons reuse guidance](https://commons.wikimedia.org/wiki/Commons%3AREUSE). The Library of Congress also maintains specifically curated [Free to Use and Reuse](https://www.loc.gov/free-to-use) sets.

### Editing real images

Creative modification should happen only when `derivatives_allowed=true`. The system should preserve the untouched original and record an edit manifest describing crop, color grade, background treatment, compositing, or AI-assisted edit. It should label edited archival material accurately and avoid edits that imply a false historical event or statement.

AI image editing should not be used to fabricate a real person at an event, place them in a false context, or turn an editorial reconstruction into apparent documentary evidence.

### Publication behavior

Attribution and license text should remain in the YouTube description and rights manifest unless a license specifically requires an on-image credit. Normal frames should continue using their full area for storytelling.

## 2. Script approval before image generation

### Assessment

This is the right change for full productions.

The current preview policy deliberately skips story and hero approvals. It completes TTS, timing repair, images, music, rendering, and QA before the first normal review. That reduces interruption, but it can spend image requests against a script the user later rejects.

The best checkpoint is after actual narration has been synthesized and measured, but before `ASSETS_PREPARING`.

### Recommended full-video workflow

```text
CREATED
  -> RESEARCHED
  -> NARRATION_PREPARING
       -> TTS measurement
       -> bounded retiming/rewrite
       -> narration validation passes
  -> SCRIPT_APPROVAL
       -> revise: preserve research, rewrite/resynthesize, return here
       -> reject: REJECTED
       -> approve: lock script and continue
  -> ASSETS_PREPARING
  -> AUDIO_ASSEMBLING
  -> AUDIO_PREPARED
  -> PROTOTYPE_RENDERED
  -> PROTOTYPE_APPROVAL
  -> PUBLICATION_CLEARANCE
  -> COMPLETED
```

This retains all three valuable human decisions:

1. approve the words after they are known to fit;
2. approve the finished audiovisual result; and
3. approve the upload metadata and rights package.

### What the script review should contain

Telegram should send only:

- candidate hooks;
- chosen hook;
- complete final script;
- measured total speech duration versus the target; and
- Approve, Revise, and Reject.

It should not send per-shot audio allowances, internal JSON filenames, provider IDs, or rights diagnostics at this stage.

### Script-lock invariant

Approval should create a `script_lock` artifact containing hashes of:

- accepted research;
- chosen hook;
- narration script;
- ordered voice-segment text;
- synthesized voice signatures; and
- measured durations.

Asset planning must refuse to start unless the current version has an approved script lock. If any locked narration changes later, the approval is superseded and image generation cannot continue until a new script approval exists.

### Prototype behavior

The recommendation is to require this new gate for `/full`. The short `/topic` prototype can retain the current one-preview path because it is explicitly a fast 10–15 second experiment and has much lower asset cost.

## 3. Closed-caption inconsistency

### Evidence

All inspected projects were created by Telegram updates in the same live repository.

| Project | Created setting stored in project | Current state |
|---|---:|---|
| `654e87e24821` | captions enabled | completed, v4 |
| `3cf7811df062` | captions disabled | completed, v2 |
| `1389c79924e0` | captions enabled | failed during v5 narrative revision |
| `23f033f8ddcb` | captions disabled | failed during v1 shot planning |
| `8011b5948852` | captions disabled | failed during v1 directing |

The current persisted Telegram preference is `false`.

The renderer obeyed each project’s stored snapshot. The failure happened before rendering: two projects were created with the wrong effective snapshot relative to the reported user preference.

### Why the exact historical cause cannot be proven

The runtime table stores only the current caption preference. It does not keep setting-change history, actor, Telegram update ID, old value, new value, or effective timestamp. The creation event also does not record which preference record was read. Consequently, current data cannot distinguish among a failed/unrecognized command, an intervening setting change, or earlier worker behavior.

There is also a concrete command mismatch: the v0.8 request used `/closed-caption`, while the implementation accepts `/closedcaption` without the hyphen. A hyphenated command is currently treated as unknown and only returns help.

### Required fix

1. Accept both `/closedcaption` and `/closed-caption` as aliases.
2. Add `/settings` to show captions and image budget and their source.
3. Append an auditable settings event for every change with actor, update ID, old/new values, and timestamp.
4. Persist a `creation_settings_snapshot` event/artifact on every project.
5. Include `Captions: yes/no` in the project-created Telegram acknowledgement.
6. Keep caption state unchanged across all revision types unless an explicit project-setting change is introduced.
7. Add tests for interleaved setting changes and multiple project creations.

## 4. Duplicate “≈740 PBq” in `1389c79924e0` v4

### Finding

Confirmed.

The declarative shot contained:

```text
shot.title  = Estimated radioactive release
shot.metric = ≈740 PBq
```

The asset planner then supplied:

```text
look.headline = ≈740 PBq
```

The `big_metric` renderer draws both `look.headline` and `shot.metric`, so the same number appeared twice. The generated source illustration contained no text; duplication was introduced entirely by the local display plan and renderer.

### Required generic fix

The asset-plan normalizer should compare normalized semantic content across every rendered slot. For a `big_metric` scene, if `headline` duplicates `metric`, set `headline=None` so the renderer falls back to the meaningful shot title. The validator should then reject any remaining duplicate headline/metric pair.

The same information-slot validation should cover:

- rank value versus headline;
- chart labels/values versus title and annotation;
- map location label versus title;
- timeline label repetition; and
- annotation versus headline, metric, and narration.

This is a generic layout/data contract, not a Kyshtym-specific rule.

## 5. `1389c79924e0` v5 — narrative validation failure

### Exact cause

The full-video narrative validator allows at most 115 spoken words.

- initial narrative: 116 words;
- repair narrative: 116 words.

The first validation error was correctly sent back to the director, but the repair remained one word over the limit. The wrapper then replaced the useful cause with the generic message:

```text
Narrative output failed validation after one repair
```

### Assessment

This should not require user intervention. It is a boundary-control defect. A model landing one word over a planning budget is not an editorial ambiguity.

### Required fix

- Prompt toward a safe target below the hard cap, for example 100–108 words with 115 as validation maximum.
- If a draft is slightly over, perform a targeted internal rewrite that specifies the exact required reduction and a lower hard target.
- Preserve cohesive-story validation; do not mechanically delete arbitrary words.
- Persist the underlying validation reason and both attempt summaries.
- Do not offer ordinary `/retry` when the same invalid responses are cached.

## 6. `23f033f8ddcb` v1 — shot-plan validation failure

### Exact causes

The two bounded shot-plan attempts failed for different reasons:

1. The first plan used a timeline label too long for the renderer’s readable safe area: `27 October 1961 — UN General Assembly appealed against proceeding`.
2. The repair shortened the labels, but created a rank-reveal shot with `intentional_silence=true` and an SFX cue. The timeline contract prohibits any voice or SFX inside intentional silence.

### Assessment

Both failures are internal plan/renderer contract problems. The second is mechanically resolvable and should not reach the user.

### Required fix

- Move mutually exclusive audio rules as close to model validation as practical.
- Normalize a truly intentional-silence shot by removing SFX; otherwise set silence to false when the cue is intentionally retained.
- Add a local timeline-label fit normalizer or layout strategy before asking the model to rewrite factual copy.
- If a repair solves the original category but introduces a new, locally correctable category, correct it deterministically rather than consuming another provider call.
- Persist and report the final actionable validation cause.

## 7. `8011b5948852` v1 — incomplete OpenAI response

### What is known

The project completed research and failed in `RESEARCHED` while directing. Its research provider responses are complete. The next OpenAI response returned with a status other than `completed`.

### What was lost

`output_text()` raises immediately for an incomplete response. The client writes to cache only after `output_text()` succeeds. Therefore the incomplete response, `incomplete_details.reason`, usage, stage, and response ID were not persisted. The exact provider reason cannot now be recovered from local artifacts.

The message says “retry or increase the output limit,” but `Settings.environment()` does not currently read a `STUDIO_MAX_OUTPUT_TOKENS` environment value even though the settings model has a `max_output_tokens` field. In practice the limit remains the built-in 10,000.

### Required fix

1. Persist a sanitized receipt for every provider response before parsing its output.
2. Record response status, incomplete reason, response ID, stage, model, usage, and configured token limit.
3. Internally retry a transient incomplete response with bounded backoff.
4. For a confirmed `max_output_tokens` reason, raise the stage-specific limit within a configured ceiling or issue a tighter prompt.
5. Load and validate `STUDIO_MAX_OUTPUT_TOKENS` from configuration.
6. Escalate only after bounded recovery fails, with the actual stage and reason.

Unlike the two semantic-validation failures above, `/retry` can make a new request for this project because the incomplete response was not cached. It still should have been handled internally first.

## 8. Invalid-response caching and misleading retry advice

### Finding

OpenAI response caching is keyed by the request payload and occurs before local semantic validation is known to have passed. When `/retry` reruns an unchanged stage:

- the same initial response is loaded from cache;
- the same repair response is loaded from cache; and
- the same validation failure occurs again without a new provider attempt.

This affects both `1389c79924e0` and `23f033f8ddcb`. Telegram nevertheless offers `/retry`, implying that retrying can help.

### Required fix

Distinguish three cache outcomes:

- `accepted` — safe to reuse;
- `rejected` — retain for diagnosis, never replay as a new attempt; and
- `transport/incomplete` — retain the receipt and follow a bounded retry policy.

Telegram should offer:

- automatic/local repair when deterministic;
- `/retry` only for genuinely retryable transport/service failures; and
- `/revise` only when human editorial direction is actually required.

## 9. Final v0.9 scope

### Phase A — reliability and script lock

1. Add full-video `SCRIPT_APPROVAL` after measured narration and before assets.
2. Add immutable script-lock validation.
3. Fix invalid-response cache classification and retry messaging.
4. Add bounded incomplete-response recovery and full response receipts.
5. Add narrative word-budget headroom and targeted internal repair.
6. Add deterministic shot-plan normalization for silence/SFX and display-slot duplication.
7. Fix duplicate metrics and add cross-slot semantic validation.
8. Add caption-setting aliases, audit history, creation snapshots, and `/settings`.

### Phase B — source media

1. Add licensed archival-media search/acquisition protocols and initial providers.
2. Extend `AssetPlan`, `ShotLook`, provenance, rights manifests, and publication credits for source images and derivatives.
3. Preserve originals and edited derivatives separately in the project image gallery.
4. Include effective caption and image-budget settings in every project-creation acknowledgement.

### Acceptance tests

- No image request occurs before a full project has an approved script lock.
- Script revision preserves research but supersedes the script lock, voices, and downstream assets.
- Prototype and publication approvals still occur after script approval.
- A public-domain or compatible licensed real image can be acquired, modified, credited, and traced back to its original.
- A source image with incompatible derivative rights is rejected before use.
- Identifiable-person and uncertain-rights warnings reach publication clearance.
- `/settings` reports the effective future-project caption and image-budget configuration.
- Both caption command spellings work, and project acknowledgements show the snapshotted value.
- A headline cannot duplicate its metric, rank, map label, or chart value.
- A 116-word draft is internally repaired without user intervention.
- Silence/SFX contradictions are normalized before rendering.
- Rejected cached responses are never replayed as successful retry attempts.
- Incomplete responses retain their reason and are retried internally before user escalation.

## 10. Recommended handling of the three currently failed projects

Do not repeatedly use `/retry` on `1389c79924e0` or `23f033f8ddcb` under the current code; their invalid responses are cached and deterministic.

- `1389c79924e0`: recover after implementing the narrative boundary fix, then resume v5 without new research.
- `23f033f8ddcb`: recover after implementing shot-plan normalization, revalidate the cached plan, and resume without new research.
- `8011b5948852`: one retry can make a fresh director request, but the better course is to implement incomplete-response receipt/retry handling first so the next failure is diagnosable.

No corrective action has been applied to these projects during this review.
