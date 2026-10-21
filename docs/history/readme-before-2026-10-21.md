# Historical README — superseded on 2026-10-21

This is an archived development reference, not the current product specification.
Use the repository-root README and the video-production architecture handoff instead.

## Long-form production and review (LF v0.5 motion)

The studio now takes a separate, backward-compatible long-form project from a topic through
research, a treatment and chapter outline, coherent chapter-based narration, and global
continuity validation. Telegram presents treatment and script approvals before any visual or paid
asset work begins, followed by a visual-plan approval before production assets are acquired.

```powershell
studio --root projects longform "How did the Moon form?"
studio --root projects run <project-id>
studio --root projects status <project-id>
```

From Telegram, use `/longform <topic>`. The first review contains the candidate hooks, chosen hook,
viewer promise, treatment, chapter outline, conclusion and audience question. After treatment
approval, the studio writes each chapter, runs local and model-assisted continuity checks, and
sends the complete script for approval. Approving that script starts a chapter-aware mixed-media
plan. It maps the locked narration to 20–35 purposeful beats, selects authentic-media candidates
where appropriate, and plans generated illustrations or code-rendered explanations elsewhere.
Telegram then sends contact sheets with rights and pre-spend cost summaries for approval. An
approved plan advances the project to `LONGFORM_VISUAL_PLAN_READY`, then production starts.

For new live long-form projects, approving the finished video now prepares a separate
publication review: YouTube title, description, selected thumbnail, source and rights
manifests, usage report, and release status. Telegram asks for publication approval;
video approval alone never approves the package or uploads it. The metadata draft uses
`STUDIO_LONGFORM_METADATA_MODEL` (default `gpt-6-luna`), while source links and music
credits are appended from saved project records. Older video-approved projects can enter
this gate without regenerating the video by running
`studio --root projects/live queue-longform-publication <project-id>` followed by `run`.

Every completed stage is durable: restarting does not repeat the research plan or an approved
checkpoint, treatment revisions reuse research, and script revisions retain the approved outline.
OpenAI live requests use background Responses with strict structured outputs; deterministic mocks
keep tests and fixture workflows offline. Burned-in captions remain off by default for long-form
projects unless the saved Telegram caption preference explicitly enables them.

Long-form research conversion now assigns citation IDs to tool-observed URLs before asking the
model for claims. The model can cite only those catalog IDs; the studio constructs source records
from the cited catalog entries and still checks claim/source references and source coverage. This
prevents an undefined citation ID from halting a first draft, but it does not replace editorial
verification that a cited source actually supports the claim.

The visual-planning phase downloads only low-resolution, rights-recorded planning previews.
After plan approval, LF v0.5 acquires production images, prepares measured narration and licensed
music, converts approved beats into a validated story-aware motion plan for each chapter, and runs
a semantic-director stage. The director may replace a suitable beat with a validated diagram;
otherwise the existing motion treatment remains. Decisions, reasons for fallback, and input hashes
are saved per chapter so a restart does not repeat valid planning work. Chapter rendering uses those
saved decisions to produce 1920×1080 chapters and one validated MP4 for review. Telegram sends the
preview when it fits its upload limit; otherwise it points to an unchanged
`review-video-vNNN-<hash>.mp4` in the project root. Approving the preview creates `final-video.mp4` in
the project root. A preview revision beginning `script:` or `narration:` rewrites the script;
other revision feedback replans visuals. The approved plan and preview require separate human
approvals. Every authentic candidate retains its exact source page,
creator, licence and attribution, and remains subject to human context and personality-rights
review.

If Telegram cannot confirm a long-form video upload, delivery falls back to a checksum-verified
copy in the project root and still sends review buttons. Because an upload timeout is ambiguous,
the reviewer should check Telegram for a possible video copy before approving; the local MP4 is
the authoritative review copy in this fallback case.

Long-form projects use a separate generated-image planning budget. The default is 30; Telegram
can persist a different 4–30 image cap for future projects with `/longformimagebudget <number>`,
show it with `/longformimagebudget`, or restore the default with `/longformimagebudget reset`.
The Shorts `/imagebudget` setting remains independent. This is a ceiling, not a target: a project
may use fewer images. Existing projects retain their saved budget until explicitly changed with
`studio --root projects/live longform-image-budget <project-id> 30`. A pending long-form preview
can be rebuilt from its approved narration and acquired media without new provider calls using
`studio --root projects/live rebuild-longform <project-id>`; the old preview remains on disk and
its approval is superseded. A previously reviewed pilot decision set can be reused with
`--semantic-decisions <directory>`; that directory must contain a `semantic-decisions.json` file
for every chapter. New live long-form projects run the semantic stage by default without this
option. The stage never changes approved narration or acquired media.

### LF v0.2.5 elastic duration

The 210-second long-form target is now soft. A substantive script that needs more room can proceed
to script approval with its original target, estimated speech duration and proposed finished
runtime displayed together. Approval durably expands the chapter timeline before visual planning.
The 300-second pilot ceiling is a planning and cost checkpoint, not a YouTube length rule. If
measured narration needs more time after synthesis, production pauses before acquiring visual
assets and Telegram offers **Keep script · extend** (with the proposed finished runtime),
**Trim script** (followed by a fresh script approval), or **Stop**. Keeping the script preserves
all spoken content and retimes the chapters without speeding up speech. The operational format
limit is 1,200 seconds; repetition, filler and digressions remain editorial failures regardless
of runtime. Projects stopped by the former `target × 0.95` narration limit can use
`/retry <project-id>` after the updated worker is restarted.

## v1.0 private YouTube delivery

Full productions now render at 1080×1920 and completed, publication-cleared projects can be
uploaded directly to YouTube as **private** videos. OAuth authorization is bound to an expected
public channel handle; each upload rechecks the authenticated channel, verifies the checksums of
the cleared root package, declares the video not made for kids, uploads the selected thumbnail,
and writes a durable `youtube-upload.json` receipt. An uncertain API outcome is never replayed
automatically, preventing accidental duplicate uploads.

Authorize this laptop once, choosing the Google identity whose active YouTube channel matches the
expected handle:

```powershell
.\.venv\Scripts\studio.exe --root projects/live youtube-auth `
  https://www.youtube.com/@CryoKnowledge
```

Then upload a completed project from Telegram with
`/uploadyoutubeshorts <project-id>`, or locally with
`studio --root projects/live upload-youtube-short <project-id>`. The command refuses incomplete,
uncleared, modified, non-private, or channel-mismatched packages. It never makes a video public.
The bot normally refreshes an expired access token automatically. To force a refresh and verify
the confirmed channel without uploading, send `/refreshyoutubeauth` from the allowlisted Telegram
account. If Google rejects the saved refresh token, repeat the local `youtube-auth` consent flow;
Telegram cannot complete Google's browser-based reauthorization.

Script approval now receives an immediate Telegram acknowledgement before the worker begins
visual acquisition, audio mixing, and rendering.

Director requests now use the Responses API's background mode. The studio saves the response ID
before polling, resumes the same request after a worker/network interruption, and checkpoints a
validated narrative before requesting its shot plan. A shot-plan retry therefore does not rewrite
or repay for narration. Timeouts are reported separately from generic connectivity failures.
Background director responses use `store=true` so a response remains retrievable after the short
temporary polling window; completed responses and usage are also retained in the local project
provider cache.

## v0.9 script lock, resilient providers, and documentary stills

Full Telegram productions now stop at a timing-validated **script approval** before acquiring or
generating any visual. The review contains the candidate hooks, chosen hook, complete script, and
measured speech duration. Approval locks the research, exact narration, voice segments, prepared
speech, timing report, and target duration. A revision reuses research, creates a new version,
resynthesizes speech, and returns to the same gate; asset preparation rejects a missing or changed
lock.

The visual planner can now choose rights-compatible Wikimedia Commons photographs for real
people, places, buildings, objects, and documented events. It retains the downloaded source, the
portrait derivative, attribution/licence metadata, and an edit manifest. Generated editorial
illustration remains the budgeted fallback. The image gallery includes both archival sources and
derivatives, while publication clearance explicitly requires human review of identity, context,
privacy, personality, trademark, and editorial use.

Provider failures are more actionable: incomplete responses receive one internal retry and a
sanitized diagnostic receipt; semantically rejected responses are never replayed from cache;
narrative repairs target safe word-count headroom; and unambiguous silence/SFX contradictions are
normalized locally. Dedicated metrics and labels can no longer be repeated as scene headlines.

Both `/closedcaption` and `/closed-caption` are accepted. `/settings` shows the caption and image
budget snapshot that will be applied to the next project, and preference changes are audited.
Model discovery and Telegram model/voice configuration are intentionally outside v0.9. See
[`docs/v0.9-scope.md`](docs/v0.9-scope.md).

## v0.8 publication clearance

New Telegram projects snapshot `/closedcaption yes|no` and, after creative-preview approval,
present one publication-clearance review containing the proposed YouTube title, description and
selected thumbnail. Approval creates a stable upload package in the project root:

`final-video.mp4`, `youtube-title.txt`, `youtube-description.txt`, `youtube-metadata.json`,
`selected-thumbnail.png`, `release-status.json`, and `usage-report.json`.

The usage report records measured requests, tokens, generated media and render time. Dollar costs
remain explicitly unknown unless providers report them or `STUDIO_RATE_CARD` names a versioned
local JSON rate card. Publication files are not promoted to the project root before clearance.

The agreed next milestone is documented in [`docs/v0.8-scope.md`](docs/v0.8-scope.md). Its scope
is limited to optional burned-in captions, publication clearance and root exports, improved
YouTube title/description metadata, and usage/cost reporting.

## v0.7 YouTube release package and music matching

v0.7 exports a reusable YouTube package for every automatically art-directed production:

- `youtube-description.txt` with a concise opening, story summary, sources, music credit,
  production disclosure and three relevant hashtags at most;
- `youtube-metadata.json` with title candidates and a private-upload payload, including the
  synthetic-media flag;
- `source-manifest.json` with the complete source/claim ledger; and
- `rights-manifest.json` with per-asset provenance and unresolved-rights IDs.

The immutable copies remain in the versioned artifact directory. v0.8 promotes them to the
project root only after publication clearance; pending and rejected packages never replace the
latest cleared root package.

Music now comes from an operator-curated local copy of YouTube Audio Library downloads. YouTube
does not offer a public music-catalog API, so the studio never scrapes Studio or invents license
metadata. The operator downloads tracks once; after import, project selection, trimming, mixing,
credits and evidence snapshots are automatic.

Download instrumental tracks from **YouTube Studio → Audio Library → Music**, preferably using
the **Attribution not required** filter. Choose files at least 75 seconds long and cover several
moods. Copy them to `music/inbox/`, then replace the example rows in
`music/inbox/tracks.yaml` with the exact title, artist, genre, mood and license shown by YouTube.
For a Creative Commons track, copy YouTube's exact attribution text.

Import the whole batch:

```powershell
.\.venv\Scripts\studio.exe music-import-batch music/inbox/tracks.yaml
```

The importer verifies audio-only media, measures duration, computes SHA-256, copies immutable
catalog assets, writes a download/license evidence record and updates
`music/youtube-audio-library.yaml`. A failed batch removes files created by that attempt instead
of leaving a partial catalog.

Selection is based first on the complete narration's requested mood tags, energy, tempo and usable
duration. A recent-use penalty breaks ties only: it never causes a worse-fitting track to replace
the best creative match. Reuse is expected when the local library is small. The exact decision
scores and selected track are persisted in each project.

## v0.6 visual storytelling

v0.6 replaces report-like frames with narration-led visual direction. Full productions declare
the purpose of every narrated shot, use full-bleed imagery wherever it supports the spoken story,
and reserve maps and charts for genuinely geographic or quantitative claims. Source citations,
music attribution and AI disclosures are exported to `publication-description.txt`; they are no
longer burned into ordinary frames or appended as a silent source card.

Code-rendered comparisons and charts show each category's formatted value and unit instead of
depending on unlabeled shapes. Maps carry a location label, and newly directed quantitative scenes
fail validation when their plotted information would not be readable without narration.

Full videos end with a complete narrated thought and a composition that returns to the opening
hook. This semantic loop remains understandable on the first viewing and reconnects naturally
when Shorts playback restarts.

Every rendered project also receives a browseable `images/vNNN/` gallery containing all composed
shot stills and their source illustrations. `manifest.json` identifies the recommended opening
frame, while every entry remains available as a thumbnail candidate.

A local Python studio that turns a topic into either a versioned 10–15-second prototype or a
complete 45–60-second vertical production. Live mode uses OpenAI web research, structured directing
and speech synthesis, with Telegram approval buttons and feedback-driven revisions.
The original deterministic YAML/mock pipeline remains available without API keys.

Live voice is AI-generated speech. Mock voice is a timing tone. v0.4 adds automatic art direction,
bounded image generation, and selection from a documented local music catalog. Geographic
outlines, typography and metrics remain code-rendered; ambience and SFX remain procedural.
Nothing is published automatically. A completed, human-cleared project can be explicitly uploaded
as private with `/uploadyoutubeshorts`; technical validation is not factual or rights clearance.

## v0.5 full productions

Full mode expands the same durable pipeline to 45–60 seconds and 6–18 shots. It is an explicit
schema choice, so existing v0.1–v0.4 projects retain their 10–15-second constraints. A ranked full
brief can set `expected_rank_count`; the studio then requires complete ranked research, one
rank-reveal per entry in reverse order, correct claim linkage and a source card before narration
or image work continues. Full mode also enforces complete coverage whenever research itself is
ranked, so omitting the optional Telegram count cannot silently produce a partial countdown.

Numeric rankings support both meanings of “best”: `descending` when larger values rank higher
(totals, scores, magnitudes) and `ascending` when smaller values rank higher (chart positions,
times, prices). Live research checks this relationship. A clear direction-only contradiction is
corrected locally and audited without another API call; non-monotonic evidence receives at most
one structured repair and then fails with a metric/tie-break instruction.

Create a full live production locally:

```powershell
.\.venv\Scripts\studio.exe --root projects/live full `
  "Five selected earthquake disasters by reported deaths" `
  --duration 55 --rank-count 5 --image-budget <explicit-approved-budget>
```

Or, through Telegram, send `/full 5 <topic>`. Omit `5` for a non-ranked documentary. Telegram
uses the configured `STUDIO_MAX_IMAGE_REQUESTS`; it does not infer or raise a budget from the
topic. The CLI accepts 1–18 only when supplied explicitly, and creation itself performs no API
work. If the worker is active, it will pick up the created project, so decide the budget first.
The default remains two image requests for backward-compatible, cost-bounded operation.

Full narration is synthesized and measured segment by segment. Reflow can grow only to the
60-second mode limit; if recordings still cannot fit, the same two-attempt internal rewrite
bound applies with a full-production word budget. If both revised versions still exceed the
limit, Telegram sends a narration-review checkpoint containing the candidate hooks, chosen hook
and complete script, with Revise and Reject controls. Human trimming feedback creates a new
version, reuses the existing research, resets the two-attempt timing cycle and resumes. An
unchanged retry is neither needed nor useful. Speech is never accelerated, clipped or silently
omitted. Five-source cards are supported in both base and editorial renderers. QA records
production mode, shot count and rank order.

Live directing now uses two bounded structured stages. The first writes one continuous,
novice-friendly voiceover before any shot boundaries exist; the second partitions that locked
script into visuals without adding, removing, paraphrasing or reordering spoken words. The chosen
hook must be one of the candidates and must use the same words in the opening narration, first
frame and captions. The later asset planner may not replace that opening headline. Full-script
validation rejects excessive fragments, missing transitions, out-of-order ranked subjects and
shot plans that do not reproduce the continuous script exactly. In v0.9 these checks feed a
timing-validated script approval before visual work. A normal directing pass uses two model responses
(narrative, then shot plan), so it can cost more than the former one-response director.

The data-only acceptance fixture is `examples/earthquake-full.yaml`: 55 seconds, eight shots,
ranks 5→1, payoff and a five-source card. The same renderer/provider interfaces are used—there
is no earthquake-specific orchestration or renderer branch. Its factual scope is deliberately
“five selected disasters using source-specific reported values,” because the evidence fixture
does not establish an exhaustive all-history ranking. See `docs/v0.5-validation.md`.

## v0.4 automatic visuals and music

New live `/topic` projects automatically prepare artwork and music after narration has passed
its internal timing checks. The generic path is: measured narration → asset plan → licensed
music preflight → generated illustrations → final mix/render/QA → one preview approval.
No new story or hero approval is requested. The manual v0.3 manifest path remains available.

To apply this pass to an existing approved, completed video:

```powershell
.\.venv\Scripts\studio.exe --root projects/live assets <project-id> <completed-version>
.\.venv\Scripts\studio.exe --root projects/live run <project-id>
```

`assets` creates a new version, preserving previous approvals/artifacts and reusing measured
speech and research. Use `run`, not another `assets` command, to resume that version.
Defaults for new projects are configured in `.env.example`:

- `STUDIO_AUTO_ASSETS=true` (false retains the procedural live visual path).
- `STUDIO_IMAGE_MODEL=gpt-image-2`, `STUDIO_IMAGE_QUALITY=medium`, portrait PNG at 864×1536.
- `STUDIO_MAX_IMAGE_REQUESTS=2` (default; validated range 1–18, per creative version).
- `STUDIO_MUSIC_CATALOG=music/youtube-audio-library.yaml` (resolved at project creation).
- `STUDIO_CLOSED_CAPTIONS=true` (default for future projects).
- `STUDIO_YOUTUBE_CATEGORY_ID=28` (Science & Technology; snapshotted for future projects).
- `STUDIO_RATE_CARD=<versioned-json-path>` (optional; absent prices remain unknown).

The planner has two persisted attempts, including at most one validation repair. Image calls
are reserved durably **before** dispatch, with one HTTP attempt per reservation; there are no
hidden transport retries. Each distinct prompt has a checksum-verified image/receipt cache.
An interrupted request without a complete receipt stops as ambiguous and is never automatically
replayed, even by `/retry`. Do not create a new revision merely to bypass an exhausted cap.
New human-requested creative revisions have a new cap and may incur new charges. Actual provider
token usage is saved; dollar costs remain unknown. Failed plan validation records its reason.

Telegram can persist a per-project cap for all projects created after the setting is changed:

```text
/imagebudget 7
/imagebudget
/imagebudget reset
```

The first command saves a cap of seven generated images for each future `/topic` or `/full`
project. The second displays the current saved/default value. `reset` returns future projects to
the configured default. Existing projects retain their immutable cap. The allowed range is 1–18,
matching the maximum number of shots in a full production.

Burned-in narration captions are also a future-project preference:

```text
/closedcaption
/closedcaption yes
/closedcaption no
/closed-caption yes
/closed-caption no
/settings
```

Titles, metrics, maps and chart labels remain visible in both modes. The setting does not create
SRT/VTT files and never changes an existing project. `/settings` reports the effective caption
and generated-image-budget values for future projects; it does not expose provider model controls.

Telegram creative reviews contain only candidate hooks, the chosen hook, the complete narration
script, the final MP4 (or hero frames at a hero gate), and the approval controls. QA reports,
timing diagnostics, JSON, source/licensing records and provider provenance remain checksum-verified
inside the project but are no longer sent as review attachments or warning-heavy chat text.

After fixing a validator bug, an operator can revalidate a selected saved response without
requesting another plan, spending image budget, or resetting attempt counters:

```powershell
studio --root projects/live recover-assets <project-id> <cached-response.json> <current-version>
studio --root projects/live run <project-id>
```

Recovery only accepts JSON inside that project's provider cache while it is preparing assets,
before a plan is accepted or any image is dispatched. The selected response is revalidated
against current shots and snapshotted with an audit event. This is an operator repair tool,
not a way to bypass plan validation or approve a preview.

The equivalent operator recovery for research accepts the original web-evidence response and
structured response, rechecks observed-URL provenance, applies only deterministic normalization,
and advances from `CREATED` without web or model calls:

```powershell
studio --root projects/live recover-research <project-id> `
  <evidence-response.json> <structured-response.json> <current-version>
studio --root projects/live run <project-id>
```

Music selection is deterministic within a **curated local catalog**, not unrestricted web
discovery. Entries require a local audio file, SHA-256, license evidence and source metadata.
Missing files, mismatched hashes, unknown rights, insufficient duration or no matching tags stop
before image generation. The selected track and its evidence are copied into the project version.
The older `examples/music-catalog.yaml` containing Absolution and Resonance remains only as a
legacy acceptance-test fixture; new live projects default to the imported YouTube Audio Library
catalog.

Generated artwork is disclosed as **AI ILLUSTRATION / NOT ARCHIVAL**. Its provenance remains
`UNKNOWN — DO NOT PUBLISH` pending rights review. Numeric-copy validation preserves displayed
values and provisional qualifiers; it does not prove all wording or imagery is factually sound.
The earthquake acceptance video remains a provisional 15-second excerpt, not an independently
verified all-history top-five countdown. See `docs/v0.4-validation.md` for release evidence.

## v0.3 presentation pass

Polish an approved, completed video without re-running its research or rewriting its narration:

```powershell
.\.venv\Scripts\studio.exe --root projects/live polish 30eb677d837f examples/earthquake-v03.yaml 4
.\.venv\Scripts\studio.exe --root projects/live run 30eb677d837f
```

The version argument is an optimistic guard: use the actual completed version from `status`.
`polish` queues a new version, retaining the approved baseline, historical decisions and source
evidence. It copies measured speech and verifies hashes before reuse. `run` resumes from durable
state, prepares music, validates layouts/audio/video and requests **one prototype approval**.
Do not repeat `polish` to retry a failed render; use `run` on the queued version.

The manifest is topic-independent: `presentation.shots` associates shot IDs with image IDs,
eyebrows, optional display headlines and annotations. The story's spoken text, timing, claims
and source references are unchanged. Display text is editorial content: ensure that overrides
do not contradict the research. `presentation.source_labels` gives compact labels for existing
source IDs. Imported images and music require SHA-256 and provenance. Inputs must be local,
relative to the manifest directory; traversal and symlink escapes are rejected. No manifest
URL is automatically fetched. Snapshotted assets, prompts and license evidence belong to the
new project version, so later edits to the original inputs cannot change the reviewed output.

Music requires a documented license, attribution, evidence file, and HTTPS license URL. One
music bed is supported; `trim_start` chooses the excerpt, which must cover the whole timeline.
The existing mixer applies gain, fades, narration ducking, normalization and intentional silence.
Missing/unknown licensing blocks library music import. Full music credit appears in the review
and QA; retain it in any eventual video description. Rights for speech/images remain separate.

The earthquake example uses a fictional AI illustration generated with the built-in image tool
(prompt in `examples/assets/earthquake-illustration.prompt.md`) and the opening of
[Resonance by Scott Buckley](https://www.scottbuckley.com.au/library/resonance/), CC BY 4.0.
See `examples/assets/resonance-license.md` for the source, download URL, attribution and changes.
The local MP3 is not a standalone distribution deliverable. Fonts and geographic outlines are
bundled with notices under `src/autonomous_studio/assets/` and included in Python wheels.

The editorial renderer supports all ten declarative shot types. It uses fixed mobile-safe text
regions with measured glyph wrapping; overflow fails before review. Map land outlines come from
Natural Earth, not a schematic grid. Hero artwork receives a restrained push-in; text stays in
its safe region. Captions show complete narration segments against measured duration, not
word-level forced alignment. Scene frames and layout checks are saved alongside the MP4 and QA.

The manual v0.3 path imports generated/selected assets through a manifest. The automatic v0.4
path above replaces this manual step for new live topics when `STUDIO_AUTO_ASSETS=true`.
The earthquake excerpt is still provisional, not a verified all-history top-five ranking.
All outputs remain review copies; technical QA is not factual or publication-rights clearance.

## Live mode and Telegram

Copy `.env.example` to `.env` and supply credentials locally; `.env` is ignored by Git.
Environment variables take precedence. Never put credentials in briefs or project artifacts.
If a credential has been shared in chat or committed, rotate it at its issuer.

```powershell
.\.venv\Scripts\studio.exe --root projects/live topic "Your documentary topic"
.\.venv\Scripts\studio.exe --root projects/live worker
```

On Windows, double-click `Start Telegram Worker.exe` in the repository root to start the same
worker without opening a terminal. The launcher uses both a process-chain check and a named mutex:
if a worker is already active, it reports the existing PID and does not start a duplicate. Runtime
output is appended to `projects/runtime/telegram-narration-review-worker.*.log`. Rebuild the
launcher after changing its source with
`powershell -ExecutionPolicy Bypass -File scripts/build_telegram_worker_launcher.ps1`.

New full productions use three intentional reviews: timing-validated script approval before visual
spending, validated-preview approval, and publication clearance. After script approval Telegram
immediately confirms that visuals, music, audio mixing, and rendering are continuing. `/topic`
retains the faster preview-first path. Approval still requires your judgment on accuracy,
pronunciation, rights, and creative quality; automated validation is technical, not editorial.
Tap Revise and reply to its feedback prompt. Only the configured chat
and allowlisted Telegram user IDs can issue commands. Send `/topic <topic>` to start remotely,
`/status <project-id>` to inspect, `/retry <project-id>` to retry a failed stage, or
`/revise <project-id> <version> <feedback>` to request a new version explicitly, or
`/uploadyoutubeshorts <project-id>` to privately upload a completed, cleared project.
`/refreshyoutubeauth` renews the saved YouTube access token without uploading.

Keep the worker running and this computer awake/online. Ctrl+C stops polling safely.
`worker --once` polls once, runs at most one project to its next gate, delivers its review,
then exits. Use the same `--root` on restart; never run two roots/workers for the same bot.
An active Telegram webhook must be removed before polling; the application never deletes it.

For local-only live reviews, use `topic`, `run`, then `approve <project-id> prototype`; run again
to prepare publication clearance, then `approve <project-id> publication` to export the package.
Existing projects retain their saved policy. To explicitly migrate a staged project, run
`studio --root projects/live preview-review <project-id> <version>` and then `run` or the worker.
This marks obsolete intermediate requests superseded, not approved. Historical approvals and
artifacts are retained. Repeating migration does not reset repair budgets. The original YAML/mock
workflow below still uses staged approvals; Python callers can select `review_policy="preview"`.
Each project stores its model IDs so restarts preserve provider choices. Long-form defaults are
`gpt-6.1-sol` for research, treatment/script, and visual beat planning; `gpt-6-luna` for
continuity auditing, visual beat repairs, and per-beat semantic layouts. The general director
setting remains `gpt-5.6-sol` for other workflows. Voice remains `gpt-4o-mini-tts`/`coral`.
Override stages with `STUDIO_RESEARCH_MODEL`, `STUDIO_STORY_MODEL`,
`STUDIO_CONTINUITY_MODEL`, `STUDIO_VISUAL_PLAN_MODEL`, `STUDIO_VISUAL_REPAIR_MODEL`, and
`STUDIO_SEMANTIC_LAYOUT_MODEL`; `STUDIO_DIRECTOR_MODEL`, `STUDIO_TTS_MODEL`, and
`STUDIO_VOICE` retain their existing roles. Existing live long-form projects can adopt these
text-stage settings using `studio --root projects/live adopt-longform-stage-models <project-id>`.
Saved semantic chapters retain their original model and are not regenerated by adoption.
API calls incur charges. Raw response usage is saved
as versioned provider artifacts; dollar cost stays unknown rather than an invented estimate.

Research uses tool-observed URLs plus a separately validated evidence schema; this is not
independent proof of factual accuracy. Review the sources and ranking limitations yourself.
Each research response is capped at six built-in tool calls and 10,000 output/reasoning tokens.
The director writes a continuous narrative and then derives its shot plan. Each stage performs at
most one validation repair. Live narration is measured and checkpointed one segment at a time. In
preview mode an overrun first triggers a local,
frame-aligned reflow (up to 15 seconds), retaining source/silent-card reading time. If recordings
cannot fit, up to two shorter-script attempts reuse the existing research. Attempts are persisted
before invoking the director so failures/restarts cannot reset the bound. Every rewritten segment
is synthesized and measured again before rendering; no intermediate approval is requested. Full
timing rewrites use 105- then 90-word ceilings and must preserve narrative connective tissue,
the audiovisual hook contract and the resolving payoff rather than clipping each shot separately.
Unchanged recordings are reused without more synthesis. Speech is never cut or sped up. All
script/timing changes accompany the final preview. If the bounded repairs cannot produce a valid
preview, production stops with one actionable error rather than sending an unvalidated review.

Timing failures do not offer `/retry`: repeating identical cached audio cannot solve an overrun.
For an older failed project, send `/repair <project-id> <version>` on Telegram, or use
`studio --root projects/live repair <project-id> <version>` and then run the project/worker.
Repair requests are version-bound. If the repair limit is reached, use `/revise` with shorter
narration feedback. Other transient failures still support `/retry`.
Successful request payloads and speech are cached locally to avoid repeating identical calls.
Ambiguous network failures may have incurred usage without returning an artifact; retries
are explicit. Telegram deliveries are checkpointed, at-least-once: a crash immediately after
sending can duplicate a message, but stale/repeated decisions cannot approve a newer version.
Project checkpoints form the durable job queue; failures pause until a retry or revision.

## Install

Use Python 3.12 or newer:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\studio.exe doctor
```

On this workstation, Python's Windows Store aliases were unavailable. The existing environment
was created using Codex's bundled Python 3.12; the `.venv` commands above now work directly.
On macOS/Linux use `.venv/bin/python` and `.venv/bin/studio`.

FFmpeg resolution: explicit Python constructor argument, `STUDIO_FFMPEG`, system PATH,
then the binary bundled by `imageio-ffmpeg`. A separate `ffprobe` install is unnecessary;
verification decodes the complete MP4 through FFmpeg. Dependencies must first be installed;
all subsequent mock runs can be offline.
`requirements-lock.txt` records the tested dependency versions for repeatable setup.

## Create and review a project

```powershell
.\.venv\Scripts\studio.exe create examples/earthquake-countdown.yaml
.\.venv\Scripts\studio.exe run <project-id>
.\.venv\Scripts\studio.exe status <project-id>
```

Read the research, story, audio direction, draft timeline, warnings and `review-request.json`
listed by `status`. Then progress through the human gates:

```powershell
.\.venv\Scripts\studio.exe approve <project-id> story
.\.venv\Scripts\studio.exe run <project-id>
# Inspect the four hero-frame PNGs and storyboard.
.\.venv\Scripts\studio.exe approve <project-id> hero
.\.venv\Scripts\studio.exe render <project-id>
# Watch prototype.mp4; inspect qa.json and the separate WAV stems.
.\.venv\Scripts\studio.exe approve <project-id> prototype
```

`approve` records a decision; `run` resumes execution. Neither command automatically approves
the next gate. The final state is `COMPLETED`, meaning the prototype was approved, not published.
Approval also creates `final-video.mp4` in the project root while retaining the immutable
versioned render.
Use `--request <approval-id> --version <n>` to bind a decision to a specific review request.
Stale IDs, wrong gates, repeat decisions, and changed/missing artifacts are rejected.

```powershell
.\.venv\Scripts\studio.exe reject <project-id> hero --message "Rework the visual direction"
.\.venv\Scripts\studio.exe revise <project-id> "Use this updated brief" --brief examples/my-topic.yaml
.\.venv\Scripts\studio.exe run <project-id>
.\.venv\Scripts\studio.exe events <project-id>
```

Revisions preserve all old artifacts and decisions, increment the creative version, and restart
research and approvals. With mock providers, free-text feedback is stored in the next story's
feedback-context artifact; **use `--brief` to apply actual changes to text, style, shots or audio**.
Provider failures preserve the last successful checkpoint and are retried with `run`. Fix a
missing executable and rerun; use a new revision to replace damaged input artifacts.

`--root <directory>` (before the command) or `STUDIO_ROOT` selects the storage root.
The default is `projects/` relative to your working directory. Keep the same root when resuming.

## What the examples demonstrate

`earthquake-countdown.yaml` contains five sourced candidates and an explicit fatalities-based
ranking. It preserves the requested topic but **does not claim an exhaustively verified global
top five**. The fixture flags historical completeness, differing estimates and Haiti's disputed
toll for editorial review. Each source includes its URL, date and paraphrased evidence notes.
The 12-second preview contains the hook, the #5 excerpt, its metric and a source card.
`earthquake-full.yaml` exercises the complete 55-second, five-item production mode.

`solar-system.yaml` exercises the same pipeline using a qualitative claim and no rankings.
To add a topic, copy a brief and replace its content. No application-code changes are required.
Full live productions default to 1080×1920 at 24 fps. Short prototypes and legacy YAML briefs
retain their declared dimensions (540×960 by default); YAML can explicitly request 1080×1920.

You can supply an explicit `shots` list to replace the default excerpt. Every shot has `id`,
`kind`, `start`, `duration`, `title`, optional subtitle, motion, claim/source references, voice
segments, sound cues, music state and intentional silence. The timeline must be contiguous
and fit the target duration. Supported kinds are `kinetic_title`, `hero_image`, `rank_reveal`,
`map`, `big_metric`, `comparison`, `timeline`, `data_chart`, `source_card` and `outro`.
Maps are labeled coordinate schematics; mock hero images are original abstract graphics.
The renderer implements the motion vocabulary as deterministic entrance/transform primitives;
these are foundational treatments, not production art direction.

Narration estimates use the configured words per minute. Overfull shots fail with a request
to rewrite; the system never accelerates audio to conceal a mismatch. Mock music is a procedural
pad, SFX are synthesized impacts, and ambience is a quiet low-frequency texture. Mixing includes
trim, gain, fades, delay, silence, music ducking under narration, and two-pass loudness normalization.
Ducking follows the declared narration intervals with configurable gain, attack and release;
the implementation uses a sample-level FFmpeg envelope, preserving deterministic output.
QA measures clipping, loudness/true peak, timing, dimensions and full-container decoding.
Speech intelligibility, emotional suitability and pronunciation remain human review items.

## Storage and extension points

SQLite `studio.sqlite3` is authoritative for projects and ordered events. `project.json` is a
rebuildable snapshot. Immutable artifact directories carry creative version and attempt IDs;
every committed artifact has a checksum, provenance, provider/model, input reference, timestamp,
cost and applicable dimensions/duration. Costs are zero for local fixtures. A crashed stage may
leave unreferenced attempt files; they are retained for diagnosis and never treated as approved.
The database and artifact root should be backed up together while the studio is idle.

Project locks prevent concurrent execution. Conditional revision updates protect against stale
writers. An approval is tied to exact artifact IDs and checksums. A later creative revision
invalidates downstream work by starting a new version while retaining the old evidence.

Provider protocols live in `providers/contracts.py`; inject replacements into `Studio(...)`.
The renderer protocol consumes a `VideoProject` and returns a success/failure union, so a future
Remotion adapter can replace FFmpeg scene rendering without changing orchestration. Local HITL
messages support text, image, audio and video artifacts plus approve/revise/reject actions.
`TelegramConfig` validates configuration and masks tokens; `TelegramAdapter` delivers reviews
after project checkpoints are committed. WAV previews use document attachments; MP4s use video.
Runtime offsets, reply correlation and delivery checkpoints are stored in SQLite.

Music provenance is explicit. Unknown licensing is labeled `UNKNOWN — DO NOT PUBLISH`.
YouTube upload is an explicit post-clearance command and is private-only. Keep API credentials in
the ignored local `secrets/` directory or environment variables; never put them in project briefs
or artifacts.

## Verification and developer demo

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests scripts
.\.venv\Scripts\ruff.exe format --check src tests scripts
.\.venv\Scripts\python.exe scripts/smoke_demo.py
.\.venv\Scripts\python.exe scripts/full_smoke_demo.py
```

The smoke demo creates its own project under `projects/demo`, records story/hero decisions as
`demo-harness (simulated)`, and stops at prototype approval. It does not impersonate human review.
The full smoke demo creates a 55-second v0.5 project under `projects/v05-demo` with deterministic
mock providers and one local preview request; it records no approval and makes no API calls.
Tests render a real video twice and compare hashes on the same dependency/FFmpeg environment.
Byte identity across different encoder, Pillow, CPU or OS versions is not promised.

Live-provider tests inject offline HTTP fixtures; they do not consume your API budget.
Future milestones include word-level caption alignment, richer motion design, broader internal
editorial QA and stronger unattended-operation controls.
