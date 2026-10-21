# Semantic design engine: production integration plan

Status (2026-10-01): the family-first registry and production path are implemented for
new live long-form visual plans. Every new code-led beat must select a registered
family/treatment at visual-plan approval; after TTS, the director binds scene content
to measured speech, vetted code/SVG assets and evidence IDs. Production rendering
rejects a missing/incomplete v2 decision rather than showing the neutral timing
scaffold. Saved v1 projects retain their compatible visual path. This document below
preserves the earlier design rationale and pilot history; it is not a statement that
the existing Marie Curie v6 has already been replanned or re-rendered.

The current cutover is **code/SVG first**. Generated semantic cutouts remain
`review_only` in the catalog and cannot be published through an automatic fallback.
Image-led beats continue to use the existing authentic/generated image pipeline.
The visual planner now rejects an archival-looking generated image that production
would silently suppress. Each applied designed beat writes its family, treatment,
selected asset ID/license/source/content hash, claim/source IDs, and measured
phrase-to-time mapping to its decision report. Chapter hashes include selected asset
content hashes, so changing a bundled illustration invalidates a reused render.

Phase-3/4 proof uses a topic-neutral scene to encode and inspect a 1920×1080 MP4,
plus regression tests for measured alignment, incomplete-decision rejection, asset
clearance, old-plan compatibility and captions. The separate audio mixer and chapter
mux path are retained. No live API-funded Marie Curie regeneration is implied by
these offline tests.

## Replanning Marie Curie on the family-first path

Project `154080f0be98` is currently v6, pending **prototype** review, with a
30-image project ceiling and no approved `design_family` values. `rebuild-longform`
only rerenders the approved plan; it cannot apply the new planner retroactively.
When the owner elects to make a new creative cut, use the normal, versioned revision
workflow from the repository root:

```powershell
.\.venv\Scripts\studio.exe --root projects/live revise 154080f0be98 "restart: Rebuild the treatment, script and visuals with family-first code scenes. Preserve the reviewed research but generate fresh narration, images and chapter renders; keep the 30-image ceiling."
.\.venv\Scripts\studio.exe --root projects/live run 154080f0be98
```

`restart:` at the pending prototype gate increments the project version, retains
reviewed research and the project image budget, creates a fresh media epoch, and
clears prior audio, visuals, semantic decisions, music and chapter renders. The run
stops at the treatment review; subsequent treatment, script and visual-plan approvals
remain human decisions. Only after visual-plan approval does the new production path
generate TTS, images, measured semantic decisions and the next MP4. The old v6 review
and files remain in the project's artifact/event history. Do not use `rebuild-longform`
for a from-scratch script/visual replan.

The isolated pilot is run with `python scripts/semantic_production_pilot.py
154080f0be98 --chapters all --render --run-name <name>`; `--resume` reuses validated
decisions and already-rendered chapters where the plan is unchanged. Decision files,
fallback reasons, frame-level motion plans, chapter QA, and the assembled review MP4
live under `projects/live/<project-id>/comparisons/semantic-production-pilot/<name>/`.
The pilot never sends a Telegram approval or overwrites the publication package.

## Starting point

The experimental engine has ten topic-neutral design families, two treatments each, a code/SVG/generated-cutout resolver, twelve new cross-topic motion studies for the six added families, and explicit review-only provenance for generated art. It accepts *authored* semantic scenes; it does not infer them from arbitrary narration. Its canvases are 960×540 silent studies, not production-quality 1920×1080 narration-synchronized scenes.

The live long-form path is different: an approved script leads to a visual plan and its approval, then TTS with measured word timings, visual asset preparation, chapter rendering, and prototype review. `VisualBeat` currently has medium-like values (`map`, `comparison`, `generated_image`, etc.) and limited display layouts; it has no separate representation of *why* a relationship deserves a particular visual family. The production motion planner maps those beats to supported shot templates, and every beat currently has one planned/acquired asset reference. The production renderer has its own motion-shot dispatch and explicitly excludes experimental guided imagery. The semantic study therefore cannot safely be enabled merely by adding its renderer to that dispatch.

Marie Curie project `154080f0be98` is v6 in `LONGFORM_REVIEW_APPROVAL`, with a pending prototype approval. It has five chapters, 32 visual beats (17 image-led and 15 code-led). The approved script, voices, evidence, assets, and approval state must be preserved during the opt-in pilot.

## The production agent's decision contract

Keep three decisions separate:

1. **What relationship is the narration explaining?** Extract a semantic proposition, important entities, spoken anchors, claim/source IDs, and whether the proposition is causal, chronological, comparative, spatial, quantitative, uncertain, or synthetic. This is a content decision, not a color or animation choice.
2. **Does a diagram improve this beat?** Compare a diagram candidate against retaining an authentic/illustrative image with labels, an existing timeline, a simple title, or no new graphic. A new family is not a quota to fill. The director must record a viewer takeaway, a selected candidate, a rejected alternative, and why the chosen visual reveals something the alternatives do not.
3. **Which treatment and subject assets are appropriate?** Choose within the eligible family based on the number of entities, the relationship to reveal, available screen time, asset form, evidence quality, and composition. Aesthetic variety is a tie-breaker, never a reason to misstate the relationship.

Proposed versioned `SemanticDesign` nested under a visual beat: `family`, `treatment`, `semantic_roles[]` (label, meaning, narration anchor, subject concept, optional verified datum), `relationship_edges[]`, `viewer_takeaway`, `depiction_mode`, `claim_ids`, `source_ids`, `asset_requests[]`, `chosen_because`, `rejected_candidate`, `uncertainty`, and `fallback`. Keep `visual_type` for medium/asset workflow and keep the approved script immutable. Version the schema; a v1 plan with no semantic design continues through the existing renderer unchanged.

### Eligibility and treatment rules

| Family | Choose it when | Treatment choice | Do not use when |
| --- | --- | --- | --- |
| Process demonstration | A repeatable method unfolds in ordered actions | Working diagram for a retained subject; transformation for changing states | The narration merely lists facts or lacks ordered anchors |
| Evidence comparison | Observations test explanations or a prediction | Competing explanations for qualitative evidence; prediction/observation only with comparable stated values and units | Two contributions are distinct but not competing; values have different denominators |
| Annotated cutaway | Physical placement or parts are essential | Guided callouts for one known subject; exploded relationship for verified part-to-part structure | Physical arrangement is unknown or a generated reconstruction could appear archival |
| Estimate/range | A bounded uncertainty materially affects interpretation | Full scale when the interval is the point; compact context tag when it qualifies another scene | A minor caveat would consume a whole shot |
| Causal chain | The sourced narration explicitly states a cause, mediator, and effect | Chain for successive consequences; mechanism for the mediating action | It would turn chronology or correlation into causation |
| Before→after | The same subject genuinely changes state | Split for direct comparison; wipe for a transformation in place | It would imply an unsupported historical “before” or improvement |
| Parallel tracks | Separate agents or processes each contribute | Swimlanes for simultaneous work; convergence for a shared result | The actions are actually one ordered process |
| Geographic journey | Place and movement explain the story | Locator for place context; route for verified movement/geodata | Only names/dates are known; schematic geometry might masquerade as a real map |
| Scale ladder | Quantities share a unit and a meaningful comparison | Proportional bars for direct comparison; stepped zoom for orders of magnitude | Units, baselines, or scope differ; values are unstated |
| Synthesis canvas | Several earlier ideas support one spoken conclusion | Mosaic for retained motifs; convergence when their relationship forms the conclusion | The narration is a miscellaneous recap without a unifying claim |

Hard gates precede scoring. An unsupported causal edge, unspoken number, guessed physical cutaway, fake geographic route, synthetic document presented as archival, or missing claim/source linkage disqualifies a candidate. For eligible candidates, score explanatory fit, evidence/asset availability, spoken-anchor coverage, legibility at 1080p, screen-time fit, and information economy. The result includes an abstain/fallback option. Family frequency can provide a small repetition penalty only after these checks. Never let an LLM's self-reported confidence bypass deterministic validation.

## Where it enters the existing workflow

1. **Before visual-plan approval:** extend the current long-form visual director's structured output with optional semantic proposals, including exact text anchors and all key labels. Use the already-approved script and evidence ledger. Validate proposal coverage, truth conditions, first-hook image policy, costs, and asset requests *before* showing the visual plan. Do not ask the model to choose frame-accurate cue times at this stage.
2. **After approval and TTS:** turn the approved semantic roles into a chapter `MotionPlan` using measured word entrances. Recompute shot bounds and reveal times, preserving each role's spoken order. Timing adaptation may move an entrance or choose an approved fallback; it may not invent or change claims. If roles land outside the shot, resegment or use the fallback and report it rather than dropping labels.
3. **Before asset generation:** resolve every nested subject concept to a project-owned code primitive, a curated licensed SVG, an approved authentic image, or an explicitly requested generated transparent cutout. A full SVG library hit is a suggestion, not automatic publication clearance. Missing concepts produce an asset request or a neutral fallback, not a misleading near-match. Generated cutouts count against the project's 30-image ceiling and carry review-only rights/depiction status. The renderer consumes immutable, versioned asset IDs and makes no live provider calls.
4. **In chapter rendering:** add a typed semantic motion shot/adapter to the production `MotionPlan` and renderer. Render at the project's 1920×1080/24-fps target with scaled geometry, fonts, SVGs, and composited cutouts; do not upscale the 960×540 test frames. Retain existing audio, measured word timing, optional caption path, music ducking, chapter mux, and QA. Preserve existing `guided_image` exclusion unless it is separately cleared.
5. **In review artifacts:** persist a per-beat decision ledger: semantic proposition, accepted family/treatment, rejected alternative, evidence IDs, asset provenance, generation cost, cue-to-word alignment, layout/contrast QA, fallback reason, and final artifact version. Make this inspectable without adding a verbose Telegram dump; surface concise visual-plan choices at its existing approval gate.

Existing modules that would change: `domain/models.py` and `providers/longform_visuals.py` for the approved design contract; `workflow/longform_visuals.py` for preview/approval and assets; `domain/motion.py` and `workflow/motion_planning.py` for measured cue conversion; `rendering/semantic_diagrams.py`, `rendering/semantic_assets.py`, and `rendering/longform_motion.py` for the production adapter. Keep old plans valid and the old path available behind a versioned feature switch.

## Marie Curie opt-in pilot

Use the existing approved v6 script, voice, music, images, and evidence. Produce new artifacts only under `projects/live/154080f0be98/comparisons/semantic-production-pilot/`. Do not approve/revise the pending Telegram prototype or overwrite its publication package. Begin with chapters 2 and 3, because they expose the hardest explanation and treatment choices, then render the full film if those pass.

Candidate decisions to audit, not assignments to force:

- `vb08`/`vb09`: measurement → separation → remeasurement could be one continuing process demonstration with retained fractions. The current two beats share a narration block, so timing must be resegmented from measured word windows; do not show a generic discrepancy chart or fabricate quantitative fraction values. This can improve the visual explanation but cannot fully repair unclear spoken wording.
- `vb12`: measurement, separation, collaboration, and the deeper question may justify synthesis only if the conclusion and question remain faithful to the speech. Otherwise retain the existing transition.
- `vb13`: “atomic property, not chemical arrangement” is an evidence/concept contrast, **not** a historical before→after claim. No false correction chronology.
- `vb15`: measurement guiding chemistry and interpretation may suit a causal/process design, provided arrows do not overstate what the sources establish.
- `vb22`: the “about twenty; accounts differ 18–20” qualifier should live on a relevant wartime-radiology visual, not become an independent scale-ladder scene.
- `vb03`: Warsaw → Paris → Sorbonne is chronology and place context; keep a timeline or verified locator. Do not draw an invented travel route.
- `vb31`: the final recap may suit synthesis, but it must cover the actual spoken points rather than stopping after two labels.
- Keep the image-led opening hook and evidence-bearing archival images image-led when they are serving the narration. A new family should not displace them merely to increase variety.

Compare the isolated pilot against the current v6 film by checking comprehension, visual engagement, factual implications, and whether images remain on screen while discussed. Avoid forcing every code beat into a new family. If the first two chapters are weak, fix the policy/renderer before rendering the full film.

## Implementation checkpoints and acceptance

1. **Decision contract and offline evaluator.** Add schema, eligibility rules, positive/negative fixtures across unrelated topics, and a Marie Curie decision ledger. Outcome: see *why* each family/treatment was selected or rejected before spending on assets. A diagram must be allowed to abstain.
2. **Asset and production-render adapter.** Resolve nested code/SVG/generated/authentic assets with license/version metadata; implement 1080p semantic shots and word-timed cues. Outcome: chapter-sized clips with unchanged narration/audio and no experimental-resolution upscale.
3. **Marie Curie chapter pilot, then full opt-in render.** Start with chapters 2–3, inspect encoded frames at important speech boundaries, then render five chapters if QA passes. Outcome: a reviewable full MP4 and decision/QA reports under `comparisons`, with v6 approval untouched.
4. **Controlled rollout.** Keep the feature off for ordinary projects until the Marie Curie pilot and at least two unrelated briefs pass. Enable per project first; retain the old renderer as fallback. Outcome: integration into the production bot only after evidence that treatment choice generalizes.

Tests must cover: unchanged script/claim IDs, schema migration for old plans, no new paid request on a render-only retry, cue order and no long blank/static tails, unspoken labels/numbers, comparable units, causal and physical-depiction gates, schematic-versus-real geography, rights/provenance, deterministic rendering, 1920×1080 video and audio QA, captions on/off, fallback behavior, and no mutation of existing approval state during an opt-in comparison.
