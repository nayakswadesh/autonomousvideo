# Long-form visual capability registry (Phase 1)

The code-rendered visual vocabulary lives in
`src/autonomous_studio/rendering/visual_registry.py`. Each family has a purpose,
use/avoid criteria, required data and asset roles, a renderer, and concrete
treatments. The matching discriminated scene contracts and PIL renderers live in
`src/autonomous_studio/rendering/semantic_diagrams.py`. The registry is designed
to be topic-agnostic: labels, source references, values, anchors, and asset
concepts are scene data, never topic-specific branches.

| Family | Treatments | Best used for |
| --- | --- | --- |
| `process` | `working`, `transformation` | How something operates or changes in stages |
| `evidence` | `competing`, `prediction`, `signal_to_inference` | Rival evidence, prediction checks, or one observed clue supporting an inference |
| `cutaway` | `guided`, `exploded`, `focal_inset` | Parts of a physical system or a magnified feature of a resolved subject |
| `range` | `scale`, `context` | Bounded estimate and uncertainty |
| `causal` | `chain`, `mechanism` | Supported cause-to-effect links |
| `before_after` | `split`, `wipe` | Evidenced states before and after a change |
| `parallel` | `swimlanes`, `converge` | Concurrent paths and an outcome |
| `journey` | `locator`, `route` | Schematic progression between places |
| `scale_ladder` | `bars`, `zoom` | Ordered magnitudes in one unit |
| `synthesis` | `mosaic`, `convergence` | Threads brought together in a supported takeaway |
| `chronology` | `event_track`, `event_cards` | Dated events without implied causation |
| `geography` | `map_locator`, `region_focus`, `verified_route` | Sourced points, bounds, or route geometry on a real map |
| `concept_contrast` | `juxtaposition`, `paired_contributions`, `correction_reveal` | Distinct concepts, complementary roles, or a supported correction |
| `quantitative_display` | `metric_reveal`, `comparable_bars`, `time_trend` | Sourced quantities, comparisons, or dated observations |
| `document_focus` | `guided_highlight`, `extracted_finding` | A documented excerpt and its finding |
| `relationship` | `hierarchy_build`, `network_build`, `nested_scope` | Supported organizational, network, or part–whole links |
| `claim_boundary` | `bounded_statement` | A supported proposition and its spoken limitation |
| `scenario_branch` | `conditional_fork` | One condition and two possible outcomes, without implied odds |
| `editorial_frame` | `kinetic_question`, `statement_reveal`, `chapter_marker` | Hook, statement, or chapter promise |
| `editorial_close` | `recap_grid`, `loop_bridge`, `community_close` | Recap, audience question, timed response choices/social invitations, or return to the opening |

That is **20 families and 46 treatments**, including the production `community_close`.
The five Point 9 treatments are
`signal_to_inference`, `focal_inset`, `nested_scope`, `bounded_statement`, and
`conditional_fork`. Their diagrams are reusable across subjects, not eligible
for every spoken idea.

## Coverage and safeguards

`LEGACY_DESIGNED_KINDS` and `LEGACY_VISUAL_TYPES` audit every existing
code-rendered long-form path against at least one family. `camera`,
`detail_reveal`, and `guided_image` are media-led motions, so they are recorded
separately in `LEGACY_MEDIA_KINDS`; they do not require a code-rendered family.
The mapping is compatibility information, not a silent conversion or a new
planning rule. New plans should choose a concrete family and treatment.

Scene validation constrains treatment-specific structure: quantitative bars
use a shared unit, trends need distinct dated points, hierarchies need valid
parent links, map points and bounds need source references, and verified routes
need supplied geometry that reaches the narrated endpoints. Spoken anchors
must occur in narration order. An uncurated asset is not silently upgraded to
publication-ready. The production workflow's source and timing gates still
apply when scenes are integrated.

`planner_catalog()` returns machine-readable choices for the family-first
director. The planner first partitions approved speech, then receives a menu
filtered by role count and reliable structural prerequisites. It chooses a family and
treatment from that menu and records its rationale. The five new treatments
require observation→inference, subject→feature, inner→system→wider,
claim→limit, and condition→possible outcome→possible outcome respectively.
The planner judges these relationships from the actual speech, not a fixed list
of topic words or conjunctions; validation checks role order and anchoring.
The semantic stage cannot rewrite approved on-screen labels or their anchors;
it supplies only renderer-specific illustration and placement. Reveal times
are aligned to measured speech. A focal inset without a resolved subject asset
fails validation; a conditional fork explicitly states that branch geometry
does not encode likelihood.

## Production integration (Phase 2)

New long-form visual plans now make a family/treatment decision for every
code-rendered beat before the visual-plan approval and paid image generation.
Image-led beats leave both fields null. The approval contact sheet displays the
choice. An old plan without these fields can still be loaded; a compatibility
mapping supplies its initial choice when it is replanned.

After narration synthesis, the production director builds **one typed scene per
designed beat** from its approved family/treatment. Each scene gets up to three
configurable validation/alignment attempts. It must preserve narration, use spoken
anchors, use ready assets, and pass measured word
timing. The chapter plan is persisted as schema v2. A v2 plan cannot use `keep`
for a designed beat, omit a designed beat, or retain a legacy code-rendered
motion shot. A newly planned image beat whose prompt would trigger the old
archival-simulation suppression is rejected **before approval**, so it must be
replanned as an authentic image or an explicitly designed beat. If a saved image
beat is suppressed by the legacy safety planner, the v2 coverage check fails
closed instead of silently publishing a code card without an approved family.

The legacy motion planner remains a **timing scaffold** so shot boundaries and
previously saved plans remain compatible. Its code-rendered output is not a
production fallback for a v2 plan. Saved schema-v1 semantic decisions retain
their previous selective behavior and hash rules; new live plans use v2. The
former `semantic_diagrams` policy flag is retained for loading old project data
but does not opt new live projects out of family-first planning.

The `geography/verified_route` treatment remains in the registry for reviewed
geometry, but new autonomous plans cannot select it yet. An approved source ID
alone does not prove that a model-generated path matches the source. The planner
must use a schematic journey until route geometry has a separate evidence
binding/review mechanism.

## Motion previews

The eight Phase 1 families have silent, illustrative MP4 studies in
`examples/visual-registry-previews/`: one clip per family and
`all-new-families.mp4` for all 19 new treatments. The treatment name appears in
the study header. These use synthetic fixture text and timing, not a live
project's approved narration or assets. Rebuild them with
`python scripts/render_visual_registry_previews.py`. They demonstrate the
renderer vocabulary; they do not establish that production planning or
speech alignment for the new families is complete.

The five Point 9 treatments have separate production-renderer previews in
`out/point9-production-previews/`. Run
`python scripts/render_visual_registry_previews.py --point9` to rebuild them.
These use synthetic fixture narration and cue timing; the approved Sun video
was not changed. Live autonomous treatment selection still needs evaluation on
a new project or an explicitly requested visual replan.
