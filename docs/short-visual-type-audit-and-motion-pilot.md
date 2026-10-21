# Short visual-type audit and object-motion pilot

## Phase 1: advisory shot-type analysis

The Short director still writes a continuous script first. Its second call makes
the renderable shot plan and now supplies one advisory `shot_type_review` per
shot. Each review compares the selected existing kind with `process_flow`,
`spatial_cutaway`, `before_after`, and `range_estimate`. The suggested kind is
either one of those four or `keep_current`.

The four candidate kinds are not part of the `Shot` union, cannot be selected
for production, and cannot alter the approved narration. Reviews are saved in
`shot-type-review.json` and summarized in the Short script-approval message.
Incomplete or absent analysis is labeled as such and does not block a valid
video. This gives reviewers evidence of demand before building new renderers.
Existing projects do not gain retrospective reviews unless their story is
regenerated.

## Phase 2: object motion in production

The production editorial renderer now animates objects in `comparison`,
`data_chart`, `timeline`, `qualitative_comparison`, `study_design`,
`big_metric`, `metric_pair`, `metric_set`, and `map` scenes. `hold` remains an
explicit opt-out. Object entrances settle early enough to leave reading time.
Timeline line-drawing is limited to labels that actually describe distinct
events; legacy mixed date/measurement timelines retain their static layout.
Text is still drawn through the production per-line contrast checker; no
fixed dark cards were copied from the exploratory pilot. Caption behavior,
mobile-safe text layout, the 4.5:1 estimated contrast gate, and full-resolution
1080 × 1920 exports remain in the regular render path. Render QA additionally
samples intermediate object entrances.

The comparison tool can render a single approved scene with its existing
imagery, shot data, caption setting, timing, and audio beside the older video.
It does not call providers, change project state, or upload.

```powershell
.\.venv\Scripts\python.exe -m tools.render_short_object_motion_comparison d480da08c77f shot_02
.\.venv\Scripts\python.exe -m tools.render_short_object_motion_comparison d480da08c77f shot_02 --production
```

Supported test families are quantitative comparison/data chart (bars grow to
their actual values), qualitative comparison/study design (concept cards enter
without fake numerical bars), timeline (event nodes and line draw), metric
cards (sequential entrance), and map (located marker pulse). A timeline pilot
rejects a mixture of dates and measures as events. Quantitative pilots require
display values on every bar; older unlabeled comparisons are deliberately
excluded until their visual data is corrected.

Comparison outputs appear under `projects/live/<project-id>/comparisons/`
in `object-motion-*` or `production-object-motion-*` folders. Each contains
`side-by-side.mp4`, `pilot.mp4`, three stills, and `qa.json`. Comparison clips
are 540 × 960; the `--production` version is rendered with the production
scene code and then downsampled solely for side-by-side review. Finished video
exports still use the project resolution. Entrances are staged within a shot,
not word-level synchronized, so editorial judgment remains necessary.

## Phrase cues and geographic maps

Renderable Short shots may now carry `entrance_cues`: one exact phrase from the
approved shot narration for each moving object. The planner can propose these
without altering the locked script. The renderer places each entrance at that
phrase's estimated word position within the measured narration recording;
invalid, missing, or out-of-order cues fail validation. This is a deterministic
phrase estimate, **not acoustic forced alignment**. Shots without cues preserve
the legacy entrance schedule. The QA sampler checks cue transitions using the
same production contrast/layout path.

Maps accept explicit `geography` of type `point`, `region`, or `route`. The
camera frames the supplied coordinates anywhere on the globe, including
cross-dateline regions/routes. A region extent defines the camera view, not a
precise historical or political boundary; a route requires authored ordered
points. Neither is inferred from a place name. Existing latitude/longitude
point maps remain valid. If a story has no time data, no time animation is
added—the camera and geometry alone tell the location/extent/journey.

Pending previews can be refreshed with a JSON mapping of shot IDs to only
`entrance_cues` and/or `geography` changes:

```powershell
.\.venv\Scripts\studio.exe --root projects\live refresh-preview PROJECT_ID VERSION "visual timing" --shot-overrides path\to\overrides.json
.\.venv\Scripts\studio.exe --root projects\live run PROJECT_ID
```

This creates a new version, preserves the prior preview, and reuses the
approved script, generated images, voice, and music without provider calls.
