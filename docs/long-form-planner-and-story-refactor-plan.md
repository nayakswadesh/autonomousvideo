# Long-form planner and storytelling refactor — deferred implementation plan

Status: **points 1–5 implemented for future long-form projects; full live proof (point 6) pending**  
Recorded: 2026-10-07  
Scope: long-form production only. **Shorts model routing and workflow remain unchanged.**

## Implementation checkpoint — 2026-10-08

### Story-first writing and audit (point 5)

- New live long-form projects pin `story_contract_version: 2` in their saved
  spec. Older saved projects default to version 1 and keep their previous
  treatment, writing and audit contract, including on revision. No approved
  cut has been migrated or advanced. The agreed models remain Sol for
  treatment/chapter writing and Luna for the editorial audit.
- The treatment specifies a real central tension, payoff and smallest essential
  claim subset per chapter. Other reviewed chapter claims remain supporting
  background, not a narration checklist. The writer and audit share a
  story-first contract: empirical essentials and uncertainty remain grounded;
  questions, bridges, analogies and scene-setting may be uncited when they do
  not invent empirical facts. Hook, title, first visual and payoff must agree.
- Only designated essential claims trigger the deterministic missing-evidence
  check. The Luna audit reports factual/misleading-impression risk,
  promise/payoff fit, newcomer clarity and narrative momentum separately in
  the persisted continuity report. An error stops the first draft under the
  zero-repair policy; the human remains the script approver. Automated audit is
  a safeguard, not proof that every sentence is true or a retention forecast.
- The studio's default output allowance is now 20,000 tokens including
  reasoning. The old 10k batching concern is a contingency to reassess from
  actual chapter usage, not a prerequisite for the first new-policy run. A
  running Telegram worker needs restart to load the new default and code.

Next proof: create a fresh long-form topic, review the treatment and complete
script at the normal gates, then inspect generated stills and rendered scenes.
Compare an unrelated topic before calling the workflow general. No live
generation is part of this implementation checkpoint.

### Resumed implementation and verification

- New-policy visual planning now uses a separate Luna speech-partition call.
  Each spoken unit belongs to one beat, the partition supplies ordered reveal
  roles, and a registry-compiled menu limits treatment choice by role capacity
  and spoken prerequisites. The selection response uses a registry enum for
  `family/treatment`; the studio derives code-asset requests rather than asking
  the model to classify them again. On-screen labels and their exact spoken
  anchors are emitted as paired cues, preventing parallel-array count drift.
- The measured-reveal middle/tail check now runs during the live audio-stage
  motion preflight for new-policy projects, before paid visual acquisition.
  Semantic scene preflight exercises the opening, each intermediate reveal,
  and final frame with strict text-fit checks. A saved version-1 repair policy
  remains version 1 when an operator changes one of its limits.
- New-policy chapters persist `spoken_unit_version: 2`, enabling a clause
  boundary at an em dash (and at a spaced en dash) in addition to sentence
  punctuation. Existing saved chapters default to version 1 and retain their
  previous unit IDs. This exposes a split point for long sentences containing
  distinct ideas without changing approved words or retroactively invalidating
  older visual plans. A read-only Marie clause-level evaluation is being kept
  separate from the approved project.
- Read-only first-draft evaluation of the approved Milky Way chapter-one script
  passed after these contract changes: 8 beats, 2 designed and 6 image-led,
  materialization and saved-TTS preflight passed, with zero creative repairs.
  Report: `out/evals/longform-visual/c3b7b0a4c7f7-ch1/report.json`.
  The earlier failed drafts were retained as provider-cache/rejection evidence;
  no live project state or human approval was changed.
- A read-only Marie Curie chapter-one first draft passed structural visual
  selection and materialization after a negated historical-image phrase was
  correctly recognized as safe. The measured-TTS preflight then stopped at
  `ch1_beat_02`: its final reveal leaves 9.1 seconds to the shot end against
  the 8.5-second target plus 0.5-second planning allowance. No image request
  was sent and no creative redraft was made. This is a genuine timing/design
  decision to resolve, not an authorization to shift the approved script.
  Report: `out/evals/longform-visual/154080f0be98-ch1/report.json`.
- A later new-policy-only read-only Marie evaluation exposed an em-dash clause
  as a legitimate beat boundary. With version-2 spoken units and image requests
  embedded in their owning beats, its first draft passed: 9 beats (3 designed,
  6 image-led), materialization and saved-TTS preflight valid, zero creative
  repairs. The historical-image safeguard was narrowed to understand negated
  coordinated lists while still rejecting positive fabrication requests.
  Report: `out/evals/longform-visual-clause/154080f0be98-ch1/report.json`.
  The live Marie version 13 remains unchanged at prototype approval.
- The final targeted long-form/semantic regression suite passed: **97 tests**,
  including the zero-repair policy, 503 transport separation, legacy planner
  compatibility, semantic rendering, and measured-speech checks. Windows
  asyncio project tests require an unrestricted local test process; sandboxed
  runs stall before the event loop begins.

Still open before declaring points 1–4 fully proven: inspect actual generated
stills and semantic scenes for visual quality/legibility (the current proof
validates exact prompts, renderer text fit, and timing but does not generate a
new review video), and run a fresh new-policy project through its full approval
workflow. The evaluated visual-selection responses used about 9.3–9.9k output
tokens each against the former 10k default. With the new 20k default,
subchapter batching is deferred unless a real larger chapter demonstrates
the need. The new policy applies to newly created long-form projects; no
existing approved cut is silently migrated.

- New long-form projects now persist an explicit per-stage creative-repair policy,
  defaulting to zero repairs. Existing projects retain their saved behavior;
  this does not resume or modify the paused Milky Way cut. The research,
  treatment, script, visual-chapter, spoken-anchor, and semantic director
  generation loops read that policy. Incomplete provider output stops on its
  first result for the new policy; HTTP transport retries remain separate.
- New long-form model routing pins Sol for treatment/script and Luna for
  research, continuity, visual planning, and semantic layout. Project stage
  traces retain model/token usage and optional rate-card estimates. The raw
  failed provider response and diagnostic are saved when a live stage fails.
- New visual drafts must supply a structured direction for generated images
  and authentic-image fallbacks. The final API prompt combines the approved
  spoken moment, focal composition, overlay space, and project film style;
  the exact payload is saved before image acquisition. Text-in-image conflicts
  and off-speech directions are rejected.
- Measured-speech motion preflight before paid image requests already existed;
  this checkpoint preserves it and adds focused policy/image-direction tests.

That initial checkpoint predated the speech-partition and treatment-menu work
recorded above. No live project or approval has been advanced by either
checkpoint.

This is the agreed plan to pick up when long-form work resumes. It supersedes no
approved treatment, script, visual plan, or prototype. Do not automatically resume
or approve the paused Milky Way project (`c3b7b0a4c7f7`). Existing projects are
not migrated in place. A creative change to an approved project requires a new,
versioned proposal and the user's normal approval.

## Objective and boundaries

Build a long-form engine whose **first creative draft is expected to satisfy the
same executable contract used by validation and rendering**. When it does not,
save the draft and a precise diagnostic and stop for root-cause investigation,
instead of relying on an automatic redraft. Preserve the approved speech and
human decisions throughout visual planning.

The video must also tell a compelling, understandable story. Truthfulness is a
mandatory boundary, **not a requirement that every spoken sentence cite an
individual research claim**. A strong, honest viewer promise, narrative
progression, clarity, and payoff should determine which researched facts belong
in speech. Do not fabricate dates, quantities, quotations, events, mechanisms,
or consequential causal claims for dramatic effect. Questions, connective
narration, scene-setting, interpretation, and clearly signaled analogies or
hypotheticals need not each carry a source ID.

YouTube's published guidance emphasizes a concise opening that fulfills the
title/thumbnail promise, value through the video, and viewer satisfaction—not a
fixed video length or a citation-dense script. Retention data is feedback after
publication, not a score that a script can guarantee:

- [Understanding content performance](https://support.google.com/youtube/answer/16559650)
- [Audience-retention moments](https://support.google.com/youtube/answer/9314415)
- [No universal ideal video length](https://support.google.com/youtube/answer/16559651)

## Current baseline to preserve

- The long-form pipeline has durable treatment, script, and visual-plan approval
  gates. TTS and measured word timings follow visual-plan approval. Production
  images and rendering follow preflight. No later stage may silently rewrite an
  approved script or switch an approved image beat to a designed scene.
- The family/treatment registry already records role capacities and ordered role
  patterns, but the planner's generic beat schema permits up to eight labels and
  anchors irrespective of its selected treatment. Some rules are enforced only
  after the model has produced a complete chapter.
- Separate chapter redrafts, local beat repairs, semantic-layout retries, and
  outer workflow retries currently exist. A local beat repair can satisfy one
  anchor check yet violate the role contract because not all invariants are
  rechecked before the patch is accepted.
- Estimated reveal timing is checked from script word allocation. Actual TTS
  timing is checked later. The first check is useful but cannot promise the
  second will pass.
- The current Milky Way script is factually careful and already approved. It is
  a regression fixture, not permission to rewrite it. As an editorial example,
  its strongest future-facing tension appears late, while the opening quickly
  turns to definitions and methodological caveats.

## 1. Baseline, failure policy, and observable diagnostics

**Change.** Inventory every long-form automatic *creative/validation repair*
path: research-plan conversion, treatment, script/continuity, visual chapter,
spoken-anchor beat, semantic layout, measured-beat adaptation, and any outer
workflow retry that regenerates creative material. Introduce a persisted,
per-stage policy such as `creative_repair_limit`, default **0** at every stage.
`0` means one initial generation and no correction generation. It does **not**
mean zero initial calls. No nested loop may bypass the effective policy.

Keep transport/service retries (for example an HTTP 503 or dropped connection)
separate, bounded, and logged. Do not classify schema errors, unsupported
semantics, invalid treatment selection, incomplete output, or failed visual
preflight as transient network failures. Preserve the raw provider response,
stage, chapter/beat ID, violated invariant, model, token usage, and artifact
version. A creative failure stops with an actionable diagnostic; an unchanged
retry is not the default recommendation.

**Outcome.** We can identify *why the first draft failed* without paying for a
second draft or losing the rejected output.

**Acceptance.** Tests prove that zero allows exactly one creative request at
every inventoried stage; that the policy survives restart; and that a simulated
503 follows the separate transport policy without triggering a creative repair.

## 2. Long-form model routing and cost visibility

**Change.** Keep `gpt-6.1-sol` for treatment/chapter structure and narration
writing. Use `gpt-6-luna` for long-form research planning and synthesis,
continuity audit, visual beat planning, semantic layout, and any future allowed
targeted repair. If a new text-model call is introduced in this work, use this
division or ask the user before assigning a model. Leave specialized image and
TTS models unchanged. Do **not** modify Shorts defaults, settings, or behavior.

Persist the resolved stage-to-model map in each long-form project. Changes to an
existing live project require an explicit versioned policy update, not a silent
change on process restart. Record request/output tokens and cost by stage and
chapter so the lower-cost routing can be evaluated against first-draft quality.

**Outcome.** The creative writing stages retain Sol while constrained and
classification-heavy stages use Luna, with an auditable per-project model map.

**Acceptance.** Routing tests cover every long-form stage, project restart, and
no change to Shorts routing. Compare Luna visual-plan first drafts against saved
fixtures for quality as well as cost; cheaper is not automatically better.

## 3. Constrained, speech-first visual planning

**Change.** Plan beat boundaries and treatment choices *jointly* from approved
speech, rather than fixing a beat and asking a model to fill a generic list.
Identify the distinct spoken ideas, ordered visual roles, and candidate
boundaries. Use the versioned treatment registry as the single source of truth
for semantic prerequisites, required data/assets, role capacity, ordered-role
patterns, rendered cue types, and layout limits. Filter to feasible choices
before asking Luna to select. Include an image-led choice when no designed
treatment honestly expresses the spoken idea.

Make image-led choices a first-class planning contract, not a generic fallback
prompt. For each generated-image beat, the planner must identify the exact
approved spoken idea it supports and specify one visual moment: focal subject,
observable action or relationship, setting, framing and subject placement,
visual hierarchy, and space needed for any renderer-added overlay. A single
still must not be asked to explain several successive ideas, an abstract
inference, and its conclusion at once. Split that material into successive
image beats or select a registered designed treatment when explanation needs
labels, a progression, or comparative data. Keep generated art free of
typography, numbers, diagrams, source labels, and fabricated archival cues;
the renderer owns factual overlays. An authentic-image fallback must obey the
same spoken-idea and composition rules without pretending to be historical
evidence.

Carry a coherent, project-specific `style_direction` into **every final image
request**, along with the beat-specific creative brief. Do not rely on the
planner merely echoing its style field inside each prompt. Adapt the Shorts
pipeline's stronger prompt contract—clear subject, deliberate composition,
negative space, and an explicit division between illustration and rendered
information—to widescreen storytelling. Do not copy its fixed charcoal/ivory
palette or always reserve the upper third: palette, lighting, focal position,
and overlay-safe region should follow the approved film style and actual scene
layout. The final prompt assembler must resolve conflicting instructions (for
example, a brief asking for typographic tiles while its wrapper prohibits
text), preserve factual and provenance constraints, and persist the exact
prompt sent to the Images API for review. The image model receives a concise,
self-contained prompt; it is not expected to infer the narration, visual
intent, or film style from a separate planning artifact.

After selection, generate exactly the chosen treatment's role slots. The
planner identifies which spoken idea should trigger each visual event; code
resolves and verifies exact, unique phrases in the corresponding spoken span.
Do not silently drop a third idea to fit a two-role treatment. Split the beat
or choose a different treatment. If speech order conflicts with a treatment's
required reveal order, swap visually reversible slots, choose a compatible
treatment, or split; never rewrite approved narration to suit the animation.

Score candidate partitions for explanation quality and feasible time, not just
minimum cuts. Preflight opening, middle, and ending visual activity. Count
meaningful motion/highlighting as visual activity when the renderer truly
supports it; do not add filler labels solely to satisfy the 8.5-second reveal
target. Keep the estimated timing check and its disclosed tolerance.

Validate each completed beat and then the complete chapter against the *same*
registry/renderer rules. With creative repairs set to zero, persist an invalid
first draft and stop; do not request a full-chapter or beat-level redraft.

**Outcome.** A two-role treatment cannot be produced with three independent
reveals, and a fixed-order treatment cannot be chosen for opposite speech order.
Generated stills are directed around a specific spoken moment and a consistent
film style, instead of broad multi-idea briefs or conflicting text instructions.

**Acceptance.** Cross-topic fixtures cover two-versus-three roles, ordered
versus reversible roles, spoken-unit coverage, exact anchors, meaningful visual
activity, honest image fallback, and no eligible treatment. Include cases where
a single sentence needs multiple beats and multiple sentences form one beat.
For image-led beats, inspect the persisted final API prompt—not only the
planner's draft—for spoken-idea fit, one clear focal moment, subject placement,
overlay-safe space, project style, and factual/provenance limits. Reject or
split a multi-idea image brief before acquisition; reject contradictory
typography instructions. Test at least one authentic-image fallback and
unrelated topics with different visual styles. Compare reviewable generated
stills against the current long-form prompt contract; prompt validity alone is
not evidence of better-looking images.

## 4. Measured-speech, assets, semantic layout, and rendering preflight

**Change.** After TTS, map approved visual events to measured word entrances
before expensive image acquisition or final rendering. Apply only
presentation-level timing changes that preserve approved ideas, treatment,
labels, and speech. If measured speech requires a different beat boundary or
treatment, stop and create a versioned visual-plan proposal for human review.

Ensure the semantic director receives fixed approved family/treatment and role
slots, not a chance to invent a new design. Resolve code/SVG/authentic/generated
assets against actual renderer capabilities, rights, and layout space. Use one
executable treatment contract for planner filtering, semantic scene validation,
and rendering. Check legibility, text fit, contrast, cue order, and cumulative
image budget; do not hide an unsupported scene behind a neutral card.

**Outcome.** An approved plan is either renderable with the measured narration
or fails before costly downstream work with a specific reason.

**Acceptance.** Tests cover measured-speed variation, long inactive passages,
scene-role mismatch, missing assets, image-budget exhaustion, layout overflow,
and preservation of approved script/image-beat decisions.

## 5. Story-first script and editorial audit

**Change.** Once the visual engine is stable, update the Sol treatment and
chapter-writing contract to optimize for a *compelling, clear, truthful story*,
not maximum claim coverage. The treatment should specify the viewer promise,
central tension or curiosity, what changes at each chapter, the essential
evidence needed to advance the story, and a satisfying payoff. The hook must
be intriguing **and** consistent with the title/thumbnail and eventual answer.
Avoid a rigid hook template or artificial cliffhangers; use the topic's real
tension and a newcomer-friendly path through it.

Separate research material into:

1. **Essential factual assertions:** dates, numbers, discoveries, disputed
   conclusions, consequential causation, quotes, or specifics on which the
   viewer's understanding depends. Check these against evidence and preserve
   uncertainty.
2. **Supporting background:** useful facts that may inform the writer or
   visuals but need not all be narrated or individually claim-linked.
3. **Narrative language:** questions, bridges, analogies, scene-setting, and
   clearly framed interpretations or hypotheticals. These can create momentum
   without inventing empirical events or masquerading as documented claims.

Change the current `missing_chapter_evidence` obligation so the script must
include only explicitly designated *essential* claims, rather than every claim
made available to a chapter. Keep unused research in the ledger and publication
notes. The Luna editorial audit should separately report (a) factual or
misleading-impression risks, (b) hook/promise/payoff fit, (c) clarity to a new
viewer, and (d) narrative momentum/repetition. Do not collapse these into an
opaque numeric “retention score” or use a hard word-per-minute trick to fake
engagement. The user remains the script approver.

For the Milky Way example, one possible direction is to contrast how ancient
encounters can be reconstructed with why a future Andromeda encounter remains
conditional. This is an editorial hypothesis, **not** approved script copy and
not authorization to rewrite the current project.

**Outcome.** The narrator tells a connected story, with factual guardrails but
without reading out the research ledger. The script may be longer when that
genuinely helps clarity; avoid filler and honor the project's approved runtime
policy.

**Acceptance.** Human review compares old and new scripts across Milky Way and
unrelated subjects for opening clarity, curiosity, explanation, coherent
transitions, payoff, and misleading implications. Automated tests enforce
essential-claim support, approval locks, versioning, and no fabricated numbers
or unsupported consequential assertions; they do not claim to predict retention.

## 6. Cross-topic proof and feedback loop

**Change.** Test the new planner against the saved Milky Way script and at
least two unrelated long-form scripts with creative repair limits at zero.
Exercise new-script generation separately from visual-planner proof, so a
script-quality change does not mask a planner regression. Compare first-draft
validity, human-rated story/visual clarity, design variety, latency, and
per-stage cost. Keep failures and decisions inspectable as artifacts.

After publication, review YouTube's intro retention, dips, top moments, and
viewer feedback for videos of comparable length and audience. A spike may mean
interest *or* confusion; interpret it alongside the video. Use observed
patterns to improve the story contract, not to invent a universal algorithm or
promise a particular retention percentage.

**Outcome.** Evidence that the engine generalizes beyond Marie Curie or the
Milky Way, with clear attribution of failures to script, planning, assets,
measurement, or renderer.

**Acceptance.** Cross-topic dry runs finish or stop on their first invalid
creative draft with precise diagnostics; approved human decisions remain
locked. Any new creative cut passes the existing treatment, script, visual
plan, and prototype approval gates.

## Implementation order and handoff

Implement points 1–4 first to stabilize the visual engine, then point 5 as a
versioned writing/audit change, then point 6 as cross-topic proof. Design the
story contract during the earlier work, but do not silently rewrite approved
scripts while stabilizing the planner. Each point should land with tests and a
documented outcome before proceeding. Do not resume the paused Milky Way run
or auto-approve its next gate merely because this plan exists.

Likely touch points include `config.py`, long-form provider routing and
`longform_visuals.py`, `longform_media.py` image-prompt assembly, the visual
registry, workflow retry/preflight modules, semantic director/integration,
and focused tests. This is a medium-to-large
refactor, not a replacement of the storage, approval, audio, image, or Shorts
systems. Exact files and estimates should be reconfirmed at implementation
time against the then-current codebase.
