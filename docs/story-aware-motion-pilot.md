# Story-aware motion pilot

The deterministic automatic planner from this pilot is now used for new long-form
production chapters. Each chapter stores its validated `motion-plan.json` and
`motion-planner-report.json` next to the rendered MP4. The comparison command below
remains opt-in: it preserves the approved source chapter and writes a separate
baseline-versus-motion package without changing project approval state or using an API.
The later guided-image/doodle experiment was not promoted to production.

## How a new topic uses it

The motion plan is JSON data validated by `MotionPlan` in
`src/autonomous_studio/domain/motion.py`. The chapter-specific subject, assets,
approved narration excerpts, claims, sources, shot timing, and editorial rationale
live in the plan; renderer code contains no Marie Curie branch. To use another
chapter, supply a plan with its project/chapter IDs and run the same command:

```powershell
.\.venv\Scripts\python.exe tools/render_longform_motion_comparison.py path\to\plan.json
```

The editorially authored chapter-1 pilot plan is
`projects/live/154080f0be98/comparisons/story-aware-chapter-1-plan.json`.
Its output is
`projects/live/154080f0be98/comparisons/chapter-1-story-aware/`:

- `baseline-chapter.mp4`: copy of the approved original;
- `motion-chapter.mp4`: new chapter with the original audio stream copied unchanged;
- `side-by-side.mp4`: original left, motion cut right, one shared audio track;
- `comparison-notes.md`: review context.

To generate a plan automatically from a chapter's approved narration, measured
speech windows, existing visual beats, and acquired assets, run:

```powershell
.\.venv\Scripts\python.exe -m tools.plan_longform_motion 154080f0be98 ch2 --render
```

Omit `--render` to inspect the plan before spending local encoding time. The
chapter-2 package is under `comparisons/ch2-auto-motion/`; it contains
`motion-plan.json`, `planner-report.json`, comparison videos, and `render-qa.json`.
The planner re-times and, when warranted, reorders beats to the narration. It
records every template choice and fallback in `planner-report.json`.

## Decision model

Each shot records an intent and the viewer takeaway, not only a visual style. A
director can choose a template according to the idea the viewer needs to grasp:

| Story job | Supported template | Behavior |
| --- | --- | --- |
| Introduce/correct a premise | `title_reveal` | Reveal spoken idea in stages |
| Establish a subject or place | `camera` | Move toward the relevant part of an image |
| Show a journey/change of location | `route_reveal` | Draw an origin-to-destination schematic |
| Direct attention to a detail | `detail_reveal` | Track a source-image detail through camera motion |
| Explain two measurements | `comparison_build` | Reveal labeled values and correctly proportioned bars |
| Explain a sequence | `process_steps` | Reveal ordered steps |
| Inspect a cited original | `document_highlight` | Highlight a source-image region |
| State a grounded fact without simulated archives | `fact_reveal` | Reveal two short phrases |
| Compare ideas without measurements | `concept_contrast` | Reveal two labeled concepts |
| Explain an unquantified discrepancy | `evidence_gap` | Show expectation and observation without invented bars |
| Connect several roles | `relationship_map` | Reveal a non-sequential relationship |

Every nontrivial reveal has a cue with a timestamp and an approved narration
excerpt. Validation rejects timeline gaps, missing/unsupported cues, unknown
assets/claims/sources, out-of-window cues, invented comparison values, and route
labels absent from the approved script. An evidence-bearing shot must carry claim
and source IDs. The measured narration windows come from the chapter QA artifact.
These are block-level windows, not word-level timestamps; fine synchronization
still requires viewing and listening to the export.

## Current boundary

The chapter-1 plan was editorially authored; chapter 2 exercises automatic,
deterministic template selection from the existing approved visual beat plan.
This is **not** autonomous art direction from a fresh topic: the upstream bot
still decides the visual beats and assets. The planner uses keyword-based
within-block timing estimates rather than word-level alignment. Generated
historical-looking portraits/documents become factual cards, and unquantified
charts become qualitative diagrams. True parallax/depth effects still require
separated layers and are not faked from a flat image.
The automatic adapter does not infer bar values from text labels (which might
be dates or incomparable units); numeric charts require an explicitly validated
data plan.

Telegram review integration and automatic use in the production pipeline have
**not** been enabled. The opt-in comparison remains 720p; production project
files, approval state, and 1080p settings remain unchanged. Only consider bot
integration after reviewing the chapter-2 comparison and its planner report.
