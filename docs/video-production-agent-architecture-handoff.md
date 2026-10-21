# Building a reusable video-production agent

Architecture handoff for a fresh coding-agent session. Written 2026-10-21 from
the implemented Autonomous Studio repository, not from the original proposal.

## 1. Read this first

The current system produces documentary Shorts and chapter-based long-form
videos. The next project may instead produce comedy, minimal line-drawn scenes,
stick-figure dialogue or another animated format. Its exact creative format has
not been chosen. Do not assume that a documentary narration over moving stills
is the desired comedy product, or that comedy is already implemented here.

The central reusable idea is:

> Separate creative decisions from durable production execution. Let models
> propose typed, renderer-compatible creative data; let code preserve approved
> decisions, resolve assets, measure time, render, validate and deliver reviews.

Build for new episodes through briefs, cast/scene data and assets, not repeated
source-code changes. Start local on the operator's laptop. Keep Telegram as a
remote approval interface and the project folder as the authoritative media
workspace. Do not introduce cloud infrastructure, autonomous public publishing,
paid third-party video services or a giant framework before the first useful cut.

This document describes three different things explicitly:

- **Implemented:** reusable behavior in Autonomous Studio today.
- **Lessons:** constraints and caveats discovered through real projects.
- **Proposed:** a simpler architecture for the new format, not a claim that the
  current code already provides it.

This is an engineering handoff, not market research. Neither a stick-figure style
nor a comedy topic guarantees a larger audience. Choose an original voice and
character identity; inspiration from minimalist comics does not grant rights to
reuse another creator's comics, characters or branding.

## 2. The production system, independent of genre

```text
Operator brief and preferences
          ↓
Creative development → approval of story/script
          ↓
Visual/performance plan → approval when the format needs it
          ↓
Asset and voice production → measured timing and scene compilation
          ↓
Scene renders → audio finishing → assembled MP4 → technical QA
          ↓
Human video review → publication package → human release clearance
          ↓
Explicit upload command → private YouTube upload + durable receipt

Every stage reads/writes: typed project state + events + versioned artifacts
Every paid/ambiguous operation has: input fingerprint + request journal + receipt
```

There are five independent responsibilities:

| Responsibility | Owns | Must not own |
| --- | --- | --- |
| Creative system | Story, dialogue, visual meaning, intended performance and shot choices | Hidden workflow transitions or arbitrary filesystem paths |
| Workflow engine | Stage sequencing, versions, approvals, invalidation, budgets and resume | Topic-specific drawing or silently rewriting approved speech |
| Providers | External model/API operations and local asset acquisition | Human approval or unbounded retries |
| Render/compiler layer | Typed scene execution, typography, motion, timing and audio assembly | A second interpretation of what the story should say |
| Review/publication layer | Correlated feedback, deliverable package, rights decisions, explicit upload | Assuming a technical pass is editorial or legal approval |

## 3. Actual stack and where to inspect it

| Building block | Current implementation and entry points |
| --- | --- |
| Runtime and CLI | Python 3.12+, Typer; `pyproject.toml`, `src/autonomous_studio/cli/__init__.py` |
| Typed contracts | Pydantic models, discriminated unions and dedicated scene schemas; `domain/models.py`, `domain/motion.py`, `domain/semantic.py` |
| Orchestration | `workflow/engine.py`; substantial format-specific helpers under `workflow/` |
| Durable state | `storage/repository.py`: SQLite, ordered events, revision checks, locks, artifact hashes |
| Research and writing | OpenAI-backed adapters and offline mocks; `providers/openai.py`, `providers/longform.py` |
| Visual planning | `providers/longform_visuals.py`, `rendering/visual_registry.py`; Shorts also have separate director/asset-planner paths |
| Semantic compilation | `providers/semantic_director.py`, `workflow/semantic_scene_contract.py`, `workflow/semantic_integration.py` |
| Still and diagram rendering | Pillow; `rendering/editorial.py`, `rendering/semantic_diagrams.py`, `rendering/longform.py` |
| SVG assets | Local catalog and Phosphor SVGs rasterized with `resvg_py`; `rendering/semantic_assets.py` |
| Generated assets | `providers/images.py`, `providers/semantic_illustrations.py`, `providers/longform_media.py` |
| Authentic images | Wikimedia Commons retrieval with license/provenance records; `providers/archival.py`, long-form discovery in `providers/longform_visuals.py` |
| Speech | Gemini and OpenAI adapters; `providers/gemini_tts.py`, `providers/openai.py` |
| Audio and alignment | WAV measurement, timing, ASR alignment, FFmpeg mixing; `audio/` |
| Video encoding and probes | One FFmpeg service; `rendering/ffmpeg.py` |
| Telegram/local review | `hitl/adapters.py`, `hitl/worker.py` |
| Publication and YouTube | `workflow/publication.py`, `workflow/longform_publication.py`, `providers/youtube.py` |
| Tests | Fixture/mocked provider tests plus real local rendering; `tests/` |

Paths in the table are relative to `src/autonomous_studio/` unless a full repository
path is given. The current repository is an evolved working application, not a
perfectly generic plugin framework. The large engine, legacy modes and compatibility
paths are reasons to borrow the boundaries rather than copy every implementation.

### Tool choice for a new minimalist animation engine

Keep Python, typed data, Pillow/SVG and FFmpeg for the first proof if they meet the
visual requirements. This stack already produces reproducible frames and audio
without a hosted video editor. For simple stick figures, deterministic vector
geometry and reusable rigs are more controllable than generating a new raster
character for every frame.

The renderer interface should permit a later alternate backend. Browser/vector
animation, a dedicated animation engine or a 3D tool can be evaluated against an
actual difficult scene later. Those are possible extensions, not installed or
tested production backends in this project. Do not promise that swapping renderers
requires no work: their scene capabilities and schema compatibility must be tested.

## 4. What existing workflows teach us

### 4.1 Full Shorts, implemented

1. Create a topic brief and snapshot models, voice, budgets and preferences.
2. Research source-backed claims and, where needed, ranking methodology.
3. Write a continuous story and choose a hook that promises a payoff without
   necessarily revealing the answer immediately.
4. Plan typed shots while preserving every approved script word.
5. Synthesize and measure narration, with bounded timing handling.
6. Ask for script approval before visual acquisition.
7. Choose music and acquire/generate the visual assets.
8. Compile the presentation, align supported entrances, mix audio and render.
9. Run technical checks and request prototype approval.
10. Prepare title/description/thumbnail/rights/usage material for publication approval.
11. Export the cleared package; upload privately only on an explicit command.

`/topic` and local staged YAML demos have different review paths. Do not use a
legacy demo's story/hero gates as the description of all live projects.

### 4.2 Long-form, implemented

1. A research-plan call proposes candidate story questions and research needs.
2. Evidence gathering and conversion create the claim/source record. Conversion
   selects from a fixed source catalog rather than inventing citation IDs.
3. Treatment writing chooses an evidence-supported story question, viewer promise
   and chapter structure. The human approves the structure.
4. Chapter writing produces a connected script with openings, progression, landing
   points and a closing audience invitation. Continuity/editorial audit distinguishes
   hard omissions/contradictions from reviewable repetition. Human script approval follows.
5. Visual planning partitions approved spoken units into purposeful beats. It
   does not impose the old fixed block/beat quotas.
6. A separate visual-selection pass sees feasible treatments and image options,
   choosing what communicates each beat best and filling its visual contract.
7. The human reviews the visual plan/contact sheets and expenditure boundaries.
8. Production synthesizes narration, obtains word timing, resolves media and
   compiles motion and semantic scenes against the approved choices.
9. Render chapters, assemble the review video, check it and request video approval.
10. Prepare a separate publication review and then the final release package.

Narration has been written before visual planning, but live measured word timing
arrives after TTS. The planner therefore works with estimates; the compiler later
binds approved anchors to the recording. Never describe pre-TTS estimates as
already measured speech.

### 4.3 What should differ for comedy

A dialogue or visual gag is not necessarily narration-first. A suitable proposed
flow is: premise → setup/escalation/payoff → dialogue and visual action together
→ performance/blocking plan → voice → measured delivery and intentional reactions
→ animation compile/render. Silence, a look, a delayed response or an off-screen
sound may be the point of the joke, not a gap to remove.

Research is optional for fictional comedy except for factual, cultural, safety,
rights or reference checks that actually matter. Do not force every fictional gag
into a source-claim ledger. Preserve provenance and non-deceptive presentation for
real-world references and acquired assets.

## 5. Contracts: the main protection against unreliable agents

The current system's typed data is one of its most reusable features. A provider
returns structured output; code validates it before another stage acts on it.
Prefer one documented responsibility and one authoritative owner per field.

### Suggested neutral project contract — proposed, not a drop-in existing schema

| Record | Minimum useful fields |
| --- | --- |
| ProjectBrief | ID, format/profile, topic/premise, audience, objective, style, aspect ratio, duration policy, budget, provider snapshots |
| StoryScript | Revision, hook/promise, ordered dialogue/narration, speaker IDs, action descriptions, payoff, continuity facts |
| Beat | Stable ID, script-span references, intent, medium, scene/treatment, rationale, duration estimate, entrance/reaction anchors |
| Screen/StagePlan | Exact visible words, entities, positions, actor actions, graph/rig requests, camera intentions, capacity constraints |
| AudioPlan | Voice segments, speaker/voice mapping, pauses, music, ambience, SFX, mix priorities |
| AssetRecord | Stable identity, content hash, path, dimensions/duration, provider/model, creation time, license/provenance, semantic subject/state |
| TimingPlan | Measured audio spans, anchor occurrences, resolved times, event intervals, intentional holds, frame-aligned boundaries |
| Approval | Project, creative revision, gate, exact artifact IDs/hashes, actor, decision, feedback, timestamp |
| ReleasePackage | Approved video/title/description/thumbnail references, rights status, upload defaults, usage and upload receipt |

Examples of mutually exclusive scene variants: `image_scene`, `designed_scene`,
`character_scene`. A character scene should not be a documentary chart with
an arbitrary `kind` value. Give it an explicit renderer contract.

Use IDs and spans for identity, not copied prose. Studio can copy narration,
titles, IDs, durations and approved labels deterministically. Do not pay a model
to copy fields it can accidentally paraphrase or omit.

## 6. Capability registry: plan only what can be rendered

The existing long-form registry defines 20 families and 46 treatments. See
[the registry reference](visual-family-registry.md) and, more importantly,
`rendering/visual_registry.py` for the current executable rules.

Each treatment needs:

- purpose and situations where it is misleading;
- required observable inputs: dates, values/units, stages, geographic geometry,
  states, part–whole structure or another actual prerequisite;
- visible slots, label lengths, reveal counts and supported ordering;
- graphic slots: required/optional/absent, and behavior when deliberately text-led;
- supported movement and parameter bounds;
- a renderer and a fast layout-validation path;
- representative examples and counterexamples.

### Two planning passes with a shared contract

1. Partition the script into coherent beats and identify their communicative intent.
   Preserve script-span coverage and do not silently rewrite approved dialogue.
2. Code filters out objectively impossible treatments. The model then sees a
   compact beat-specific menu of remaining choices, plus the image option, and
   chooses/fills the best feasible scene.

Registry, prompt contract and validator must share the same definitions. A label
capacity hidden in a downstream renderer will create repeated paid failures.
Do not require the model to read hundreds of general rules and infer which apply.

The second pass must be allowed to design its reveals to fit the chosen treatment;
do not accidentally freeze an arbitrary earlier reveal count as immutable story
semantics. If no treatment can express the beat, choose an appropriate image or
report that the boundary needs review. Do not force an ill-fitting design.

For comedy, a separate small performance registry could start with `two_shot`,
`reaction_closeup`, `prop_reveal`, `offscreen_reveal` and `callback_return`.
These are proposed examples, not existing Studio families. Define actual rigs,
action/reveal capacities and timing before exposing them to the creative agent.
Fewer fully working choices beat a large vocabulary of unsupported promises.

## 7. Ownership between planning and compilation

The current design deliberately moves creative decisions upstream:

- The script writer owns spoken words and story progression.
- The visual planner owns the image/design decision, family/treatment, visible
  copy, speech anchors, graphic intentions and the rationale.
- The semantic stage should fill only genuinely missing renderer-specific asset
  or composition information. The studio injects known fields from the plan.
- The asset resolver owns obtaining a concrete, ready asset for each approved request.
- The timing/compiler layer owns measured timestamps and frame placement.
- The renderer executes those decisions; it does not improvise a new story.

Some old compatibility paths still exist. Do not cite their existence as permission
for a new compiler to replace an image scene with an unapproved text card, invent
new labels or swap a chosen treatment. A media timing scaffold is not a second
creative director.

For the new bot, an illustration resolver may choose a ready local asset or fulfill
an approved generated request. It should not decide the punchline or add dialogue.

## 8. Asset sourcing, identity and continuity

### Implemented source types

1. Local code-drawn primitives for simple structural shapes.
2. A local SVG catalog for recognizable subjects/icons.
3. Generated illustration cutouts when an approved diagram needs a specific asset.
4. Full-screen generated editorial images for image scenes.
5. Authentic documentary imagery with retained originals and rights metadata.

These have different jobs. A generic square may be a valid container, but is not
a substitute for a promised human, instrument or physical process. Conversely,
an abstract idea does not always benefit from a decorative icon.

The installed catalog's matching is not an all-purpose semantic search service.
Do not assume any natural-language subject will resolve to a relevant SVG. The
current generation path includes explicit request markers and readiness checks;
read the resolver before changing its behavior. Generated assets have budget,
transparency, provenance and content restrictions separate from local icons.

### Proposed comedy asset model

Prefer a stable `CharacterDefinition` containing proportions, line weight, color,
face options, attachment points, layer order and reusable poses. An episode casts
these IDs and animates parameters rather than regenerating the person on each shot.

Props should also have semantic IDs distinct from temporary paths. A state such
as `door_open` may reuse one rig with a rotated component; a broken door may need
a different visual asset. Decide reuse from actual asset identity and parameters,
not from brittle equality between free-form descriptions.

Start with code/SVG stick-figure rigs and a tiny curated prop set. Generate a
missing reusable prop only when necessary, then normalize and cache it. Do not
generate every frame or regenerate a recurring cast member for each line.

### Rights and provenance are part of the asset

Keep source page, creator, license/version, attribution, original bytes, derivative
operations and contextual restrictions. Public access is not a reuse license.
A photograph of an object from an accident is not necessarily a photograph of
the event at the narrated moment. Store that distinction and do not imply otherwise.
The current Commons adapter accepts public domain, CC0 and compatible CC BY;
it conservatively excludes licenses such as CC BY-SA. That is application policy,
not a claim that other licenses can never permit reuse.

## 9. Speech, performance and timing

### Current behavior

- New live Shorts and long-form projects default to Gemini 3.8 Flash TTS/Aoede.
  OpenAI remains an alternative. These are saved deployment choices, not universal
  model recommendations or guaranteed API IDs on every account.
- `/ttsprovider` persists the choice for future projects. Already-created projects
  retain their provider/model/voice and prepared recordings.
- The provider receives approved speech verbatim, with delivery direction separate
  from spoken text. WAV results are cached and normalized before assembly.
- Shorts cue times use estimated word positions within each measured recording.
  This was intentionally retained; it is not word-perfect transcription alignment.
- Long-form uses ASR-derived word timing. Anchors are resolved against approved
  speech and measured words. Ambiguous repeated phrases and ASR omissions remain
  known edge cases; local historical repairs are not proof of a universal solution.

### Proposed performance-aware flow

Give every utterance a stable ID, speaker, approved words, delivery intention and
intentional pause/reaction opportunities. Synthesizing a coherent exchange or
performance block can preserve tone better than treating every short sentence as
an unrelated voice request. Keep the granularity configurable and test it.

After synthesis, map events to the actual recording:

```text
utterance start → anticipation → spoken phrase → visible action
               → punchline landing → reaction hold → next utterance
```

Not every action belongs exactly on a word. An anticipatory movement may precede
a line; a reaction can deliberately follow it. Represent this as an anchor plus
an offset/hold policy, not as a new fabricated spoken cue.

Avoid global speed-up to fit a target. Preserve intended silence and landing
points. Distinguish a mistakenly empty screen from a purposeful comic hold.
Synthetic delivery still needs listening tests; a provider name alone does not
guarantee comedic performance, emotional range or correct pronunciation.

For repeated speech, prefer span IDs/occurrence indices in a new contract. Do not
resolve every repeated word using a naive first-substring match and expect robust
behavior across scripts.

## 10. Rendering: deterministic execution of a declarative scene

Keep scene content separate from `render_frame(scene, time)`-style execution.
Convert time to bounded progress and apply explicit easing/motion parameters.
Render images/characters, overlays and captions as separate layers where useful.
Cache SVG rasterization and other static work rather than repeating it per frame.

The current Shorts production compositor is `rendering/editorial.py`. There is
also a legacy/mock renderer; its existence does not mean new stories should be
validated against the wrong typography contract. Long-form designed scenes use
`semantic_diagrams.py` with a family-first contract and production presentation.

Use the same layout/font-fitting implementation for preflight and final output.
Validate actual readable bounds, wrapping, safe areas, contrast and intermediate
animation states. Keep text code-rendered rather than asking image generation to
spell titles correctly. The current single-line centering behavior applies to
specific card layouts, not every text slot in the application.

For proposed stick animation, typical reusable parameters are head/body/limb
transforms, gaze, mouth state, hand/prop attachment, squash/stretch limits and
camera framing. Preserve temporal continuity: consistent hand positions,
screen direction and prop ownership matter more than the number of effects.

Finish via a dedicated FFmpeg service: deterministic frame encoding, scene/chapter
assembly, audio muxing and full-container inspection. Prefer argument arrays over
shell-built commands. Keep output dimensions and frame rate explicit.

## 11. Audio finishing

Voice and sound are first-class timed assets, not a soundtrack pasted on at the end.
The current mixer handles trim, gain, fades, delay, silence, music ducking and
two-pass loudness normalization. It checks duration and clipping and retains stems.

Narration normally outranks music. In comedy, an intentional SFX or pause may be
the gag; model that priority explicitly rather than globally burying every sound
under speech. Do not invent a studio audience or laughter track as a default.

Music is operator-curated local YouTube Audio Library material in the current
deployment. The bot does not have a public music-catalog API or download arbitrary
tracks autonomously. Import measures audio and records evidence. Selection matches
mood first and uses recent repetition only as a weaker preference. Costs/licenses
must not be guessed; attribution belongs in release metadata where required.

## 12. Durable workflow and storage

SQLite is authoritative. The `project.json` file is a convenience snapshot, not
the only source of truth. Store a project revision counter, creative version,
stage, pending approvals, provider-policy snapshot and artifact references.

Each transition commits an ordered event with stage, version and relevant inputs.
An artifact has a unique ID, relative path, role, creative version, hash,
provider/model, input reference, timestamps, dimensions/duration and known usage.

Keep attempts append-only:

```text
projects/live/<id>/
  project.json
  artifacts/vNNN-<stage>-<attempt-id>/...
  provider-cache/...
  images/...
  comparisons/...
  review-video-vNNN-<hash>.mp4
  final-video.mp4                    # approved root convenience export
  youtube-title.txt / youtube-description.txt / selected-thumbnail.png
  release-status.json / usage-report.json / youtube-upload.json
```

The SQLite database sits at the project-root level shared by projects. Back it up
with the artifact tree while idle; include WAL state consistently rather than
copying only one potentially active database file.

Use a project lock for writes/execution, a separate worker lock for Telegram, and
revision checks to reject stale writers. A local single-worker system does not
need a distributed queue to be resumable.

### Dependency-aware invalidation

Hash inputs by actual dependency: provider/model/voice and speech affect a voice
recording; changing a research/writing model should not invalidate that recording.
An asset cache key includes its content request and generation settings. A render
depends on scene data, assets, timing, renderer version and output settings.

Changing one prop image should not rewrite the episode. Changing an approved
spoken line does require dependent timing/visual review. Preserve earlier valid
artifacts and human decisions where dependencies are unchanged; do not globally
adopt a new policy into older approved projects without explicit intent.

## 13. Request journals, budgets and failures

Before a paid request, persist its fingerprint and reservation. Save its response
and receipt before downstream normalization. A timeout may mean the provider
completed work that the client did not receive. Do not clear its journal and
blindly resubmit.

The current OpenAI Responses integration persists background response IDs and
can retrieve an existing operation. Image/TTS adapters have their own caching
and journal behavior; not every provider offers recoverable server jobs.
New long-form projects default to zero creative repair attempts after the first
draft; old projects can retain different saved policies. Transport retry behavior
is separate and must be inspected rather than inferred from that number.

| Failure | Correct first response |
| --- | --- |
| Invalid generated structure/copy | Inspect output and shared contract. Fix missing generation guidance or a wrong validator; do not default to a whole creative redraft. |
| Timeout/network/503 | Determine whether a request ID/receipt can be recovered; retry only under the operation's idempotency policy. |
| Moderation rejection | Recognize a provider content rejection, not a timeout. Preserve the diagnostic and review a suitable replacement or authorized archival alternative. |
| No acceptable archival result | Report a search/rights/context miss, not proof that no usable photo exists anywhere. A separately reviewed broader search can help. |
| Missing required graphic | Check planner intent, resolver match and generation eligibility. Do not silently render a generic box. |
| Alignment failure | Distinguish wrong anchor, repeated phrase, transcription omission and genuinely missing/garbled audio. |
| Ambiguous upload | Inspect YouTube and saved receipt; do not create a duplicate video. |

For this operator, stop a live run on a new failure and provide RCA before changing
anything. Do not auto-approve creative gates. Ask which model to use before adding
or changing a model-backed operation unless the operator has explicitly authorized
the precise routing for that change. Do not bury a new paid call inside validation.

## 14. Validation should be useful, shared and proportional

Hard validation is appropriate for broken references, missing required assets,
unsafe paths, invalid units, unreadable geometry, clipped audio and absent approvals.
It must not reject harmless unused metadata that the treatment never displays.
The renderer's actual slots—not a shared arbitrary string maximum—determine copy fit.

Timing estimates are imperfect. The current long-form system reports some reveal-gap
breaches as warnings before and after TTS; the old 8.5-second rule should not be
mistaken for a universal cinematic law. Keep objective structural constraints and
human editorial judgment distinct. A comedy engine should not flag a purposeful
reaction hold as an invalid story just because no new label appears.

Apply whole-chapter checks to the assembled chapter, not to each partial beat.
Let optional supporting copy remain data when a selected treatment has no slot
for it. Never select a document treatment merely because research used a document.
Normalize safe mechanical differences in code; semantic changes require the
proper creative owner and possibly a new approval.

## 15. Human-in-the-loop review and Telegram

The existing worker polls Telegram, allowlists operators, persists update offsets,
correlates replies/callbacks to review IDs and versions, and delivers text,
images/audio/video with approve/revise/reject actions. Rendering runs off the
polling loop so it does not freeze command acknowledgements.

For a new bot, keep approvals few and meaningful: script/performance concept,
animatic or production plan where needed, finished video, then release package.
Send an immediate acknowledgement when an approval queues longer work.

Do not automatically compress an oversized review just to fit Telegram. Write a
stable local copy and send its location and review controls. A cloud-drive adapter
can come later. Preserve correlation for ordinary reply feedback; do not answer
a valid revision reply with generic help text.

Preferences apply to future projects and are snapshotted at creation. Project
feedback is not automatic cross-project learning. If reusable comedy preferences
or a character bible are desired, make that a deliberate reviewed data feature.

## 16. Publication is a separate workflow

Video approval does not mean rights clearance or permission to upload. Generate
an engaging but accurate title/description, selected thumbnail, source/rights
manifests, AI disclosures, usage report and release status. Promote cleared
exports to predictable root filenames while retaining immutable source artifacts.

The current uploader checks the confirmed OAuth channel and checksums, sends the
publication-selected thumbnail, and records a durable private upload. Its payload
includes saved audio/metadata language, no paid promotion, not-made-for-kids and
synthetic-media disclosure. Those defaults describe this documentary deployment;
a future comedy bot must not assume that every animated video is or is not for
children, or that paid promotion is impossible. Set declarations honestly.

No automatic public publishing is implemented. The operator currently reviews
private uploads manually. External upload authorization must be separate from
local rendering and must never be inferred from approval of an earlier script.

## 17. Security, distribution and licensing

- Read keys from local environment/ignored `.env`; use `SecretStr`-style masking.
- Keep OAuth client files, refresh/access tokens and channel authorization under
  ignored `secrets/`, never in example briefs, logs or generated prompts.
- Sanitize errors; do not log HTTP authorization headers or full credential bodies.
- Keep provider output untrusted: it cannot direct filesystem writes or commands.
- Resolve artifact paths under the intended project root; reject traversal.
- Stage source from an explicit allowlist, inspect the Git index and scan for
  credentials before committing. `.gitignore` alone is not a secret scanner.
- Do not publish live project databases, voice recordings, provider caches,
  private feedback, downloaded music or test-generated project trees by default.
- Preserve third-party font/icon/map licenses. The existing repository CC0 license
  does not automatically cover those assets or all produced videos.

Laptops sleep and lose network access. Plan for restart and recovery, not continuous
uptime. Stronger secret management, backups, operational monitoring and deployment
hardening can be added without changing the creative contract.

## 18. What to test before an expensive live run

1. Schema/contract tests for every scene variant and registry capability.
2. Offline script→plan→asset→render workflow using fixtures and tiny audio assets.
3. Save/reload at every approval and production boundary.
4. Changed-input cache invalidation and unchanged-input reuse.
5. Rejected/stale approvals, interrupted requests and ambiguous outcomes.
6. Single-line and wrapped copy, long names, punctuation, duplicates and contrast.
7. Entrances at actual/estimated anchors and intermediate animation frames.
8. Audio duration, silence, ducking, clipping and normalization.
9. Video dimensions, frame rate, decodable streams and readable mobile output.
10. Rights records, final-thumbnail selection and upload idempotency using fake APIs.
11. At least three unrelated stories; no topic-specific hard-coded repair in production.
12. A small real-provider pilot with a pre-approved model and explicit spend cap.

Byte-identical renders are meaningful only within a pinned environment; a different
FFmpeg/Pillow/OS stack can change hashes. Technical passing does not establish
that a joke lands. Listening/viewing reviews remain essential.

## 19. Proposed build sequence for the new bot

| Phase | Outcome | Proof before progressing |
| --- | --- | --- |
| 0: Creative format | One premise, original cast/style, target viewer and a short example script; decide dialogue vs narration | Human agrees it is the intended kind of comedy; no audience-size promise |
| 1: Offline renderer proof | One reusable rig, a prop, two camera framings and a short setup/payoff scene using local audio | Looks intentional in motion; redraw another premise without changing rig code |
| 2: Declarative episode engine | Typed script/action/scene contracts, capability registry and deterministic compiler | A second episode works by changing data only |
| 3: Durable HITL pipeline | SQLite/events/artifacts, resume, approvals, minimal Telegram control | Kill/restart at a checkpoint without losing decisions or duplicating work |
| 4: Live creative and voice providers | Bounded writing/performance requests and cached speech | One approved short pilot, stable characters and intelligible delivery |
| 5: Production finishing | Timing, SFX/music, visual/audio QA, thumbnail and release package | Multiple unrelated episodes pass without topic-specific patches |
| 6: Private delivery | OAuth-bound upload, receipts and explicit release controls | One privately uploaded video manually checked before routine use |

These are outcomes, not elapsed-time promises. A first MVP should be a short
locally rendered scene, not an eight-minute fully autonomous comedy episode.
Do not build all existing documentary treatments before proving one funny,
well-timed performance with a much smaller vocabulary.

## 20. Starter instructions for the next coding-agent session

The following is a suggested handoff prompt, not authorization for this session
to create another bot or spend on provider calls:

> Read this architecture handoff and inspect the referenced contracts where
> available. We are starting a separate local-first comedy-video bot; the exact
> format is not yet decided. Reuse the architectural principles—typed creative
> data, capability-aware planning, deterministic rendering, durable state,
> provider adapters, approval gates, provenance and request journals—but do not
> copy documentary narration/claim requirements blindly. First help choose and
> prototype a small original visual/performance format. Propose the smallest
> episode contract and offline renderer proof. Keep future episodes data-driven.
> Ask before introducing paid calls or selecting/changing their models. Preserve
> human decisions, stop on new failures for RCA, and do not auto-approve or upload.

### Open decisions to settle with the operator

- Dialogue sketches, visual gags, narrated comic strips, or another format?
- Original recurring cast or self-contained episodes?
- Vertical Shorts, horizontal videos, or both after the first proof?
- One narrator or multiple character voices, and what performance budget?
- What constitutes an acceptable original style and first reference scene?
- How much human script/comedy review is expected per episode?

Do not block useful offline design exploration on every question. Make reversible
assumptions explicit and get agreement before a materially different creative path.

## Further repository references

- [Current README](../README.md): setup, commands, outputs and current behavior.
- [Visual family registry](visual-family-registry.md): vocabulary and historical integration notes.
- [Shared Gemini TTS](longform-gemini-tts.md): provider settings and cache behavior.
- [YouTube upload defaults](youtube-upload-defaults.md): API payload and final thumbnail handling.
- [Long-form planner/story roadmap](long-form-planner-and-story-refactor-plan.md): design history, not all current behavior.
- [Long-form v4 feedback](sun-long-form-v4-revisions.md): unresolved/evolved production observations.
- [Maintenance instructions](../AGENTS.md): failure investigation and approval policy.

Historical documents can contradict newer implementation. Verify the source and
saved project version before changing behavior; do not revive an obsolete quota,
repair loop or fallback merely because an old plan described it.
