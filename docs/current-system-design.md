# Autonomous Studio v1.0 — Current System Design

Status: current-state reference as of 2026-09-21  
Scope: the behavior implemented in this repository today, including the Telegram-driven live workflow and the older staged/local workflow

## 1. What the system is

Autonomous Studio is a local-first production pipeline for vertical documentary Shorts. A user supplies a topic through Telegram or the command line. The studio then:

1. researches the topic with cited web sources;
2. writes a continuous narration and turns it into a declarative shot plan;
3. synthesizes and measures the narration;
4. repairs timing within fixed limits;
5. asks for script approval on full productions;
6. acquires licensed documentary stills and/or generates visual assets;
7. selects licensed music from a local YouTube Audio Library catalog;
8. creates sound effects and ambience;
9. renders and technically validates a vertical MP4;
10. asks the user to review the finished preview;
11. prepares YouTube metadata, a thumbnail, usage reporting, and a release record;
12. asks for final publication clearance; and
13. exports a complete upload package into the project root after approval; and
14. on an explicit upload command, verifies and uploads that package to the confirmed YouTube
    channel as a private, not-made-for-kids video with its selected thumbnail.

The application is generic. Topics, claims, rankings, scripts, shots, assets, and approvals are stored as data. There is no topic-specific renderer or earthquake-specific branch in the workflow.

The system never uploads implicitly. Publication clearance assembles the reviewed local package;
the separate `/uploadyoutubeshorts` command performs the external private upload. Public or
unlisted publication, scheduling, and post-publication analytics remain outside the system.

## 2. The shortest useful mental model

There are five major parts:

```text
Telegram or CLI
      |
      v
Workflow engine / durable state machine
      |
      +---- OpenAI research, directing, TTS, and generated images
      +---- Wikimedia Commons rights-aware documentary stills
      +---- local YouTube Audio Library catalog
      +---- procedural SFX and ambience
      +---- Pillow scene compositor + FFmpeg audio/video renderer
      |
      v
SQLite state + immutable versioned artifacts
      |
      v
Telegram preview approval -> publication clearance -> root upload package
```

The workflow engine is the coordinator. Providers do specialized work, but they do not decide project state. SQLite is the authoritative project record. Files under the project directory are artifacts; `project.json` is only a convenient snapshot of the SQLite record.

## 3. Two operating modes that coexist

The repository contains two review models. This distinction explains why the state enum contains more gates than the Telegram bot normally shows.

### 3.1 Live Telegram mode: `preview`

Projects created by `/topic` or `/full` use:

- `pipeline = live`
- `review_policy = preview`
- automatic asset generation enabled when configured
- publication clearance enabled

The studio completes research, story development, narration measurement, and bounded timing repair
internally. A `/full` project then requests approval of the measured script before any visual is
acquired or generated. After approval it completes assets, audio, rendering, and technical QA and
asks for a review of the finished audiovisual preview. If that is approved, it asks for a separate
publication-clearance review. `/topic` retains the faster one-preview creative path.

There are no routine story or hero-frame approvals in this path. A narration review appears only
when the system cannot fit the narration after two internal rewrite attempts.

### 3.2 Local/legacy mode: `staged`

Projects created from a YAML brief with `studio create` default to:

- `pipeline = mock`
- `review_policy = staged`
- publication clearance disabled unless explicitly configured in code

This path exposes intermediate story and hero approvals. It exists for fixtures, tests, local development, and backward compatibility. It is not the normal behavior of new Telegram projects.

## 4. Telegram command reference

Telegram accepts messages only from the configured chat and allowlisted user IDs. Commands and callbacks from any other sender are ignored without revealing project information.

### `/start` or any unrecognized message

Telegram does not have a separate `/start` implementation. Any message that does not match a supported command returns the help text listing the available commands.

### `/topic <topic>`

Creates a 10–15 second prototype; the current Telegram default is 12 seconds.

Example:

```text
/topic How Saturn's rings may disappear
```

Behavior:

- creates a live project;
- snapshots the current model configuration;
- snapshots the current image-budget and caption preferences;
- queues research;
- uses the validated-preview review policy;
- enables the publication-clearance gate.

A prototype is intentionally an excerpt, not a compressed full countdown. Its narration target is 18–34 words and its shot count is 2–6.

### `/full [rank-count] <topic>`

Creates a complete 45–60 second production; the current Telegram target is 55 seconds.

Examples:

```text
/full How the Milky Way is built
/full 5 The most destructive earthquakes in recorded history
```

If the first token after `/full` is an integer and more text follows, that integer is interpreted as the expected ranking count. Otherwise the whole message is the topic.

For a ranked full production, the system requires complete research coverage and presents each
rank once in reverse order. Full narration targets 100–108 words with a 115-word hard validation
ceiling, uses 6–18 shots, and ends with a semantic loop back to the opening idea.

### `/imagebudget`

Shows the effective image-generation budget for **future** projects and whether it came from the saved Telegram preference or the configured default.

### `/imagebudget <1-18>`

Persists the maximum number of image API requests allowed for each future Telegram project.

Example:

```text
/imagebudget 6
```

Important details:

- the value applies only to projects created after the command;
- it is a maximum, not a requirement to generate that many images;
- the asset planner can reuse one generated illustration across multiple shots;
- the setting is stored in SQLite and survives worker restarts;
- it has no effect if automatic assets are disabled in configuration.

### `/imagebudget reset`

Clears the Telegram override. Future projects use `STUDIO_MAX_IMAGE_REQUESTS`, whose built-in default is 2. Existing projects remain unchanged.

### `/closedcaption`

Shows whether future projects will contain burned-in narration captions and whether the value is saved or is the configured default.

### `/closedcaption yes|no` and `/closed-caption yes|no`

Persist the caption preference for future projects. Existing projects remain unchanged.

This setting controls narration caption cards rendered into the video. It does not remove necessary editorial labels, titles, metric values, chart labels, or map labels.

### `/settings`

Shows the caption and generated-image-budget values that will be snapshotted by the next project,
including whether each value comes from a saved Telegram preference or the environment default.
It does not expose or modify provider models.

### `/uploadyoutubeshorts <project-id>`

Uploads a `COMPLETED` project to the OAuth-confirmed YouTube channel. The bot acknowledges the
queue immediately and later returns the private YouTube Studio link. Before any network write,
the publisher verifies the release is cleared for the current creative version and rehashes the
root video, metadata, thumbnail, and release record against their immutable artifacts.

Uploads are always `private`, declare `selfDeclaredMadeForKids: false`, suppress subscriber
notifications, and set the selected thumbnail. The command is idempotent for a confirmed upload.
If the upload outcome is uncertain, it records an `ambiguous` receipt and refuses automatic
replay until the operator checks YouTube Studio. A thumbnail failure retries only the thumbnail,
never the video body.

### `/refreshyoutubeauth`

Forces a refresh of the saved YouTube access token, verifies that the authorized channel still
matches the confirmed channel, then saves the new token atomically. It does not upload a video.
Normal uploads already refresh expired access tokens automatically; use this command to check
authorization ahead of an upload. If the refresh token itself is revoked or expired, the bot
reports that local `studio youtube-auth` browser consent is required. The token is never sent to
Telegram.

### `/status <project-id>`

Returns the project ID, current workflow state, creative version, and current error text, if any.

Example:

```text
/status 654e87e24821
```

The state is an internal checkpoint, not a percentage-complete estimate. For example, `PROTOTYPE_APPROVAL` means a validated video exists and is waiting for a decision.

### `/retry <project-id>`

Clears a retryable stage error and lets the worker resume from the last durable checkpoint.

Use it when an external or local operation failed transiently, such as a provider transport error or renderer failure. It is not accepted while a review is pending, after completion/rejection, or for unchanged overlong narration. It also does not reset bounded attempt counters.

Retry resumes the same creative version. It is not a request to make a different video.

### `/repair <project-id> <version>`

Queues a narration-timing repair for the exact project version.

Example:

```text
/repair 654e87e24821 3
```

The command is accepted only for a narration timing failure. It never speeds up or cuts speech. The workflow either reflows scene durations or rewrites the complete narration within the production limit. A stale version is rejected so an old message cannot modify newer work.

New Telegram projects ordinarily perform the two bounded timing repairs automatically; this command mainly exists for explicitly failed or older projects.

### `/revise <project-id> <version> <feedback>`

Creates a creative revision using free-form feedback. The version argument prevents delayed Telegram messages from revising a newer project version.

Example:

```text
/revise 654e87e24821 3 Make the explanation less technical and give the ending more emotional resolution.
```

Normal revision behavior:

- marks any current review as revised;
- increments the creative version;
- clears generated voices, asset plans, images, audio, presentation, and publication selection for the new version;
- returns the project to `CREATED`;
- adds the feedback to the live research requirements;
- reruns the full creative pipeline.

Two exceptions are described below:

- narration-gate feedback preserves accepted research and returns to story development;
- structured publication metadata feedback changes metadata without rerunning creative providers.

### Review buttons: Approve, Revise, Reject

Every review has a unique approval ID bound to one project version and a fixed set of artifact hashes.

- **Approve** accepts that exact review and advances the workflow.
- **Reject** records the decision and moves the project to `REJECTED`.
- **Revise** asks Telegram to collect a reply tied to that exact review.

After pressing **Revise**, reply to the bot’s “Reply with feedback…” message. A normal standalone chat message is not revision feedback; if it does not match a command or a force-reply prompt, the bot sends its help text.

Buttons on old review messages are rejected after the project has advanced because the approval is no longer pending.

### Metadata-only publication revisions

At the publication gate, a reply beginning with one or more explicit fields performs a metadata-only revision:

```text
title: A stronger title
description: A revised complete description with up to three hashtags.
thumbnail: 2
```

Accepted fields are `title:`, `description:`, and `thumbnail:`. Thumbnail can be a candidate number, filename, or filename stem. This path keeps the same creative version and reuses the research, narration, images, music, and video.

Any other publication feedback is treated as a creative revision and reruns the creative pipeline from `CREATED`.

## 5. What Telegram sends at each review

### Full-video script review

After measured narration passes, the bot sends candidate hooks, the chosen hook, the complete
script, measured speech duration versus timeline duration, and Approve/Revise/Reject buttons. No
image has been acquired or generated yet. Approval locks the research, script, ordered segments,
prepared voices, and timing result. Revision reuses research and creates a new measured version.

### Normal preview review

The bot sends:

- candidate hooks;
- the chosen hook;
- the complete script;
- the rendered MP4; and
- Approve, Revise, and Reject buttons.

The user is expected to judge story clarity, factual accuracy, visual meaning, pronunciation, pacing, and overall quality. The system’s QA is technical; it is not a substitute for editorial judgment.

### Narration review

This is an exception path after two unsuccessful internal rewrite attempts. The bot sends the candidate hooks, chosen hook, complete script, and only Revise/Reject actions. Approval is deliberately unavailable because the measured narration does not fit the hard production limit.

Revision feedback should identify what may be shortened or how the story should be rewritten. That revision preserves the completed research.

### Publication-clearance review

The bot sends:

- the proposed YouTube title;
- the complete YouTube description;
- a compact usage summary;
- the selected thumbnail; and
- Approve, Revise, and Reject buttons.

Approval creates the root upload package. The user should check title, description, thumbnail, facts, pronunciation, and asset-rights notes before approving.

## 6. Live project workflow, step by step

The normal `/topic` and `/full` path is:

```text
CREATED
  -> RESEARCHED
  -> NARRATION_PREPARING
       -> optional internal timing revision(s), new version each time
       -> exceptional NARRATION_APPROVAL if two rewrites still do not fit
  -> SCRIPT_APPROVAL (`/full` only)
       -> revise: new script version, reuse research, measure again
       -> reject: REJECTED
       -> approve: ASSETS_PREPARING
  -> ASSETS_PREPARING
  -> AUDIO_ASSEMBLING
  -> AUDIO_PREPARED
  -> PROTOTYPE_RENDERED
  -> PROTOTYPE_APPROVAL
       -> revise: new creative version, back to CREATED
       -> reject: REJECTED
       -> approve: PUBLICATION_CLEARANCE
  -> PUBLICATION_CLEARANCE
       -> metadata revise: stay at the same gate/version
       -> creative revise: new creative version, back to CREATED
       -> reject: REJECTED
       -> approve: COMPLETED + root upload package
```

### Step 1: Project creation — `CREATED`

The studio creates a 12-character project ID. For Telegram requests, the ID is deterministically derived from the Telegram update ID, making duplicate delivery of the same update idempotent.

The new project snapshots:

- topic, duration, mode, audience, and expected rank count;
- research/director/TTS model IDs and voice;
- caption preference;
- image model, quality, request budget, and music-catalog path;
- review policy and publication-clearance policy.

Changing global settings later does not silently change those project-level choices.

### Step 2: Research — `CREATED` to `RESEARCHED`

The live research provider makes a bounded OpenAI Responses request with web search required and a maximum of six tool calls. It asks for primary sources, uncertainty, contrary evidence, and a concise evidence ledger.

The evidence response is converted to a strict `ResearchBrief` containing:

- sources with URLs and evidence notes;
- claims with source references, confidence, status, and caveats;
- subjects to cover;
- optional ranks; and
- an explicit ranking metric, method, scope, limitations, and direction.

Only URLs observed in the web-search tool output may appear as source records. A model-generated URL that was not observed causes validation failure.

For numeric rankings, `direction=descending` means the largest value is rank 1; `ascending` means the smallest value is rank 1. A clear direction-label mismatch is repaired mechanically without changing ranks or values. Ambiguous or non-monotonic rankings fail rather than being guessed.

The structured response receives one bounded model repair if schema or semantic validation fails. Accepted research, findings, repair notes, and provider traces become versioned artifacts.

### Step 3: Narrative development — `RESEARCHED` to `NARRATION_PREPARING`

The director uses two distinct stages.

First, it writes one continuous spoken story before defining shot boundaries. For full videos it must:

- orient a viewer who knows nothing about the subject;
- use a concrete chosen hook as the exact first spoken sentence;
- make the candidate hooks distinct;
- create causal or temporal flow instead of a list of facts;
- use varied sentence lengths and avoid excessive fragments;
- retain uncertainty from research;
- cover all ranked subjects in countdown order when applicable;
- provide a payoff that resolves the opening promise; and
- end with a complete semantic-loop sentence.

Second, it partitions that locked narration into declarative shots without rewriting it. The opening frame title and the first spoken sentence must use the chosen hook’s exact words. Every factual visual references claims and their sources. Full stories cannot use a source-card ending; sources belong in publication metadata.

Narrative and shot-plan model requests run as durable background Responses. The create response
ID and current status are journaled under the project's provider cache before polling begins. A
temporary polling failure leaves that journal intact, so `/retry` retrieves the same response
instead of creating a duplicate. A validated narrative has a separate prompt-bound checkpoint;
if shot planning fails or the worker restarts, the next attempt reuses that exact narrative and
continues at shot planning. Director background responses set `store=true` to remain retrievable
beyond OpenAI's short temporary polling window, and are copied into local immutable usage records
after the stage commits.

Both the continuous narrative and shot plan each receive at most one model repair after validation failure.

### Step 4: Narration synthesis and measured timing — `NARRATION_PREPARING`

Each voice segment is synthesized at normal speed (`1.0`) and stored with a content signature based on voice settings, text, delivery direction, pronunciation notes, and model snapshot. Timing itself is excluded from the signature, so an unchanged recording may move on the timeline without being regenerated.

The studio measures the real WAV duration instead of trusting word-count estimates. It compares each recording with its scene allowance and creates a complete timing report.

The repair policy is:

1. If all speech can fit within the mode maximum, reflow scene durations to measured speech.
2. If the minimum required timeline exceeds 15 seconds for a prototype or 60 seconds for a full video, rewrite the complete narration while preserving research and narrative continuity.
3. Permit at most two internal rewrite attempts.
4. Never accelerate, crop, or conceal speech.
5. If two rewrites still cannot fit, stop before asset generation and ask the user for targeted narration feedback.

An internal repair creates a new creative version. Unchanged research is copied into the new version with provenance. Unchanged measured voice can be reused only when its signature still matches.

### Step 5: Full-video script approval — `SCRIPT_APPROVAL`

For `/full`, successful timing validation creates a `script_lock` artifact before requesting
approval. Asset preparation verifies that the approved lock still matches the current research,
narration, voice segments, synthesized voice checksums and durations, timing validation, and
target duration. Any mismatch stops the pipeline. `/topic` skips this gate.

### Step 6: Asset planning — `ASSETS_PREPARING`

Asset planning happens only after narration has passed measured timing. This avoids spending image requests on a script that will be rewritten.

The planner produces:

- one to the configured maximum number of image briefs;
- a visual strategy and display treatment for every shot;
- a series label;
- music tags, mood, energy, and tempo; and
- rationale for the plan.

The validator enforces generic rules, including:

- every shot has exactly one display plan;
- image requests stay within budget;
- generated-image strategies actually have images and vice versa;
- the opening display headline preserves the chosen hook exactly;
- display copy cannot introduce unsupported numbers;
- provisional/working uncertainty labels cannot be removed;
- narrated metrics use image-backed metric visuals;
- comparison/chart scenes show category labels and formatted values;
- map scenes require a visible location label;
- full semantic-loop stories may have at most one intentional text-only shot;
- redundant annotations that merely repeat narration or the headline are removed; and
- the opening and ending visual plan supports the semantic loop.

The planner gets two attempts. The validation error from the first attempt is fed into the second. After two failures the project stops at the durable asset-preparation checkpoint. An operator can revalidate a cached provider response after a validator fix without making another API call.

### Step 7: Music selection before image spending

The asset workflow validates music before dispatching image generation.

Tracks are local files imported from the YouTube Audio Library with catalog metadata, checksum, duration, license type, attribution requirement, and evidence file. The selector scores tracks by requested tags, mood words, energy, and tempo. Creative fit is primary; recent use and library source are tie-breakers. Reuse is allowed when a track is still the best match.

The chosen source and license evidence are copied into the immutable project version. The audio is decoded and checked for clipping before image calls continue.

### Step 8: Visual acquisition and image generation

The planner chooses an archival source for suitable real people, places, buildings, objects, and
documented events, or a generated illustration for conceptual scenes and fallback. Wikimedia
Commons acquisition accepts Public Domain, CC0, or CC BY JPEG/PNG assets, keeps the exact
downloaded source and a portrait derivative, and records attribution, rights metadata, source
URLs, checksums, and the crop/resize operation. A human must still review identity, context,
privacy, personality, trademark, and editorial use before publication.

Each generated image prompt asks for an edge-to-edge vertical editorial illustration with no embedded text, numbers, labels, watermarks, maps, or charts. The local renderer adds factual text and data so it remains readable and verifiable.

Image calls use a content-addressed fingerprint. Before the HTTP call, the request is reserved and persisted. A successful response must be exactly one valid 864×1536 PNG under the size cap and receives a durable receipt and checksum.

If a job journal exists without a complete receipt, the studio does not automatically replay the request because the first request may already have incurred usage. Cache hits do not consume another request. Generated visual provenance is currently classified as unknown for publication-rights review.

### Step 9: Presentation and publication candidates

Generated visuals are linked to shots through artifact IDs. A presentation manifest stores headlines, annotations, strategy, series label, source labels, and music credit.

Before rendering, the studio also creates immutable candidate artifacts for:

- YouTube title;
- YouTube description;
- YouTube metadata;
- source manifest; and
- rights manifest.

These records are not burned into normal video frames.

### Step 10: Audio assembly — `AUDIO_ASSEMBLING` to `AUDIO_PREPARED`

The audio timeline contains four stem types:

- voice;
- music;
- sound effects; and
- ambience.

For each input, the mixer verifies duration and checksum, checks clipping, trims it, resamples to 48 kHz mono, applies gain and fades, delays it to the timeline start, and pads/trims it to project duration.

The default hierarchy is voice over story SFX over music over ambience. Music follows per-shot states (`bed`, `rise`, `reduce`, or `silence`) and musical structure beats. During narration windows, an attack/release envelope ducks music. Intentional-silence shots mute the combined mix.

The final mix is loudness-normalized, checked for exact duration, and rejected if any clipped samples remain.

### Step 11: Visual rendering and QA — `AUDIO_PREPARED` to `PROTOTYPE_RENDERED`

The current renderer is local Pillow composition plus FFmpeg, not Remotion.

Supported declarative shot kinds are:

- `kinetic_title`
- `hero_image`
- `rank_reveal`
- `big_metric`
- `map`
- `comparison`
- `timeline`
- `data_chart`
- `source_card` (legacy/staged content only)
- `outro`

The renderer composites full-bleed artwork, typography, editorial overlays, charts/maps where requested, motion, and optional narration captions. A world map is rendered only for a `map` shot; it is not a general fallback for arbitrary scenes.

Before expensive encoding, every real caption state is sampled and passed through text-layout validation. The renderer creates one composed still per shot, then encodes all frames as H.264 vertical video and muxes AAC audio into an MP4.

Technical QA verifies:

- the complete file decodes;
- expected width, height, and duration;
- video and audio streams exist;
- final loudness and clipping;
- contiguous validated timeline;
- story scope and rank order;
- measured narration validation for the current version; and
- editorial layout validation when a presentation exists.

The generated QA record intentionally says `publishable: false`; only human publication clearance can authorize the upload package.

### Step 12: Thumbnail gallery

Every composed shot still is copied to:

```text
<project>/images/vNNN/
```

The gallery also contains generated illustrations, archival derivatives, archival source images,
and a manifest. The first composed shot is the automatic recommendation, but publication feedback
can select any candidate.

This folder is deliberately easy to browse so the same images can be evaluated for thumbnails outside the workflow.

### Step 13: Preview review — `PROTOTYPE_APPROVAL`

The engine will not create this review unless the current version has:

- a rendered prototype;
- passing audio/video QA;
- passing narration timing;
- passing full-story scope validation when applicable;
- passing asset validation for automatic assets;
- presentation and image artifacts; and
- required publication-candidate artifacts.

The approval stores all current artifact IDs. Before a decision is applied, every referenced file is rehashed. Missing or modified files invalidate the approval.

### Step 14: Publication clearance — `PUBLICATION_CLEARANCE`

After preview approval, the studio chooses the recommended or user-selected thumbnail, aggregates provider usage, creates a pending release status, and sends the publication review.

The proposed description contains:

- the opening hook;
- a short thesis/payoff summary;
- a call to action;
- factual source URLs;
- music title, artist, library, and attribution when required;
- a generated-media disclosure; and
- at most three hashtags, including `#Shorts`.

The title is capped at 100 characters and the description at 5,000 characters. Metadata defaults to private upload, Science & Technology (category 28), the project language, not made for kids, and synthetic media declared. `STUDIO_YOUTUBE_CATEGORY_ID` configures and snapshots the category for newly created projects; existing projects retain their saved value.

The usage report aggregates unique provider response IDs, token data when supplied, web-search calls, TTS requests/characters/seconds, image reservations/successes/cache hits/ambiguous requests, and render time. Costs remain explicitly unknown unless providers report them or a versioned local rate card is configured.

The release record includes technical checks, the human actor and approval ID, approved artifact hashes, rights status, unknown-rights artifact IDs, and unresolved blockers. The current implementation permits the human to approve despite unknown-rights items; that approval is an explicit human acknowledgement, not an automatic rights determination.

### Step 15: Completion and root package — `COMPLETED`

Publication approval atomically copies seven convenience files into the project root:

```text
final-video.mp4
youtube-title.txt
youtube-description.txt
youtube-metadata.json
selected-thumbnail.png
release-status.json
usage-report.json
```

The immutable versioned artifacts remain the records of origin. Root files are convenient “latest cleared version” exports and can be replaced only by a later successful clearance. Pending or rejected publication reviews do not overwrite an earlier cleared package.

An explicit YouTube upload adds `youtube-upload.json` to the project root and an immutable receipt
to the versioned artifact store. It records channel ID, video ID, privacy, made-for-kids setting,
source hashes, thumbnail result, and final/ambiguous status.

## 7. Version, revision, and attempt counters

These terms have different meanings:

| Term | Meaning |
|---|---|
| Project ID | Stable 12-character identity for the whole production. |
| Creative version | Increments when story/timing/creative output changes. Artifacts and approvals are version-bound. |
| Repository revision | Increments on every persisted state/event update and supports optimistic concurrency. |
| Timing rewrite attempt | Counts bounded internal narration rewrites; maximum two per cycle. |
| Asset-plan attempt | Counts planner attempts; maximum two per version. |
| Image request | Counts reserved image API requests against the project’s snapshotted budget. |

A normal retry does not reset these bounds. A deliberate creative revision starts a new version and resets version-specific preparation state.

## 8. Durability, concurrency, and restart behavior

The repository root contains `studio.sqlite3` with three tables:

- `projects`: authoritative serialized `VideoProject` records;
- `events`: append-only transition and operation history; and
- `runtime`: worker offsets, delivery checkpoints, Telegram preferences, and reply correlations.

SQLite runs in WAL mode. Each save uses optimistic revision checking. Each project also has a nonblocking OS file lock. Artifact paths are constrained to the project directory and files are verified by SHA-256 before resumed work and approval.

The Telegram worker stores:

- the next `getUpdates` offset;
- a processed marker for each Telegram update;
- one checkpoint per review text/media/button delivery;
- the reply-message-to-approval mapping;
- saved image-budget preference; and
- saved caption preference.

This gives restart-safe, at-least-once behavior. If the worker dies, committed project state remains. On restart it resumes incomplete projects, redelivers only uncheckpointed review items, and does not rely on conversation memory.

The worker uses long polling and refuses to run if the bot has an active webhook. A named OS lock prevents two workers using the same bot/runtime root. The Windows `Start Telegram Worker.exe` launcher also checks for an existing worker process before starting one.

The worker continues polling Telegram while one production runs in a background thread, but it launches only one production job at a time. Other queued projects wait durably.

## 9. Artifact and directory layout

A typical live root is `projects/live`:

```text
projects/live/
  studio.sqlite3
  <project-id>/
    .lock
    project.json
    provider-cache/
      <content-addressed OpenAI response>.json
      tts-<fingerprint>.wav
      images/
        <fingerprint>.png
        <fingerprint>.json
        <fingerprint>.vNNN.job
    artifacts/
      v001-brief-<attempt>/
      v001-created-<attempt>/
      v001-researched-<attempt>/
      v001-narration_preparing-<attempt>/
      v001-assets_preparing-<attempt>/
      v001-audio_assembling-<attempt>/
      v001-audio_prepared-<attempt>/
      v001-prototype_rendered-<attempt>/
      v001-publication_clearance-<attempt>/
      ...
    images/
      v001/
        01-<shot-id>.png
        source-01-<image-id>.png
        manifest.json
    final-video.mp4                 # only after clearance
    youtube-title.txt               # only after clearance
    youtube-description.txt         # only after clearance
    youtube-metadata.json           # only after clearance
    selected-thumbnail.png          # only after clearance
    release-status.json             # only after clearance
    usage-report.json               # only after clearance
```

Attempt-directory suffixes are unique, so a failed operation does not overwrite a prior artifact. Only artifacts recorded in the persisted project are committed workflow inputs.

## 10. Provider boundaries

The domain and workflow depend on provider protocols rather than concrete APIs:

| Boundary | Current live implementation | Local/test implementation |
|---|---|---|
| Research | OpenAI Responses + web search | deterministic research fixture |
| Director | OpenAI structured narrative + shot plan | deterministic mock director |
| Voice | OpenAI TTS | deterministic timing tones |
| Images | OpenAI Images | deterministic generated frames |
| Music | local YouTube Audio Library catalog | procedural mock audio |
| SFX | procedural local audio | procedural local audio |
| Ambience | procedural local audio | procedural local audio |
| Renderer | Pillow + FFmpeg | same local renderer |
| HITL | Telegram long polling | file-based review request |

Provider results are represented by typed records and artifacts containing provider, model, input reference, timestamp, latency, cost when known, dimensions or duration, SHA-256, and provenance.

## 11. Declarative project model

The renderer does not receive a free-form “make a video” prompt. It receives a validated `VideoSpec` containing:

- the researched brief;
- hook, thesis, payoff, complete narration, and narrative beats;
- ordered, contiguous shots with start and duration;
- claim/source references;
- voice segments and pronunciation notes;
- music state and sound cues;
- optional intentional silence;
- presentation choices and generated-image references;
- semantic loop behavior; and
- closed-caption policy.

Discriminated unions separate shot kinds, audio asset kinds, claim kinds, and provider-result kinds. Invalid combinations fail before rendering.

This separation is what allows a future renderer adapter to replace Pillow/FFmpeg without changing research, directing, approvals, or the core project format.

## 12. Failure and recovery semantics

| Situation | What the system does | User action |
|---|---|---|
| Temporary provider/renderer failure | Persists the last successful checkpoint and sanitized error. | Use `/retry <id>` after the cause is resolved. |
| Narration exceeds a shot but can fit under mode maximum | Creates a new version with measured scene timing. | None; automatic in preview mode. |
| Narration cannot fit under 15/60 seconds | Rewrites the complete narration, preserving research. | None for two attempts. |
| Two narration rewrites still fail | Sends a narration review; no preview or image spending. | Press Revise and give targeted feedback, or Reject. |
| Asset planner fails first validation | Sends the validator error into one repair attempt. | None. |
| Asset planner fails twice | Stops at `ASSETS_PREPARING`. | Creative revision, or operator cache recovery after a validator fix. |
| Image request outcome is ambiguous | Refuses automatic replay to avoid double spending. | Inspect cache; create a deliberate revision if another generation is wanted. |
| Review button is stale | Rejects the callback. | Use the latest review. |
| Artifact was removed or changed | Hash verification fails. | Restore the original bytes or deliberately revise/regenerate. |
| Worker restarts | Resumes from SQLite and delivery checkpoints. | Normally none. |

## 13. Security and trust boundaries

- Secrets are read from process environment or the local `.env`; known key prefixes only are loaded.
- API keys and Telegram tokens use secret-bearing types and are not written into project artifacts.
- YouTube client credentials and refresh tokens live under the ignored `secrets/` directory and
  are never copied into project artifacts.
- OAuth authorization is pinned to a confirmed channel ID resolved from the configured public
  handle. The authenticated channel is checked again before each upload.
- Telegram access requires the exact configured chat and an allowlisted sender ID.
- Webpage content, topic text, human feedback, and asset briefs are treated as data, not executable instructions in provider prompts.
- Research sources must be observed by the search tool before acceptance.
- Artifact paths cannot escape the project directory.
- Provider caches and local input assets have size/type/checksum validation.
- Review decisions are bound to project, gate, version, approval ID, and artifact hashes.

The system still relies on the laptop’s filesystem security and on responsible storage of `.env`. It does not provide secret rotation, remote vault integration, encrypted artifact storage, or user roles beyond the Telegram allowlist.

## 14. Current boundaries and architectural gaps to keep in mind

These are current facts, not a proposed next-iteration scope. They are useful lenses for reviewing bugs and feedback.

1. **The laptop is the production host.** The worker, SQLite database, files, and FFmpeg all run locally. Work stops while the computer is off, asleep, disconnected, or the worker is not running.

2. **There is no external job queue or service supervisor.** A lock prevents duplicate workers, but process recovery depends on restarting the executable. The worker serializes production jobs.

3. **Technical QA is stronger than editorial QA.** The system can prove dimensions, duration, stream presence, timing, clipping, and layout. It cannot independently guarantee factual truth, pronunciation quality, emotional coherence, or that a visual communicates the intended idea.

4. **Provider repair loops are intentionally bounded.** Research, narrative, shot planning, narration timing, and asset planning stop after small fixed numbers of repairs. This controls spending and looping but can expose model/schema brittleness to the user.

5. **A general creative revision is expensive.** Most free-form revision feedback restarts research and the entire creative pipeline. Only narration-gate revisions preserve research, and only explicit publication fields take the metadata-only path.

6. **Generated-image rights remain unresolved by the program.** The rights manifest marks unknown provenance restrictions and asks the human to acknowledge them. The studio does not perform legal clearance.

7. **Music is limited to the locally imported catalog.** Matching quality depends on the tags, moods, energy, tempo, duration, and license evidence supplied during import.

8. **The current renderer is a template-driven 2D compositor.** It supports reusable scene types and motion, but it is not a general video editor, generative-video system, or Remotion implementation.

9. **Thumbnail selection is simple.** The first composed shot is recommended automatically; there is no click-through prediction, face/object saliency model, or automated A/B test.

10. **YouTube metadata is deterministic and local.** The title/description generator follows limits and disclosure rules but does not query live YouTube trends, search demand, or channel analytics.

11. **Publishing is deliberately narrow.** The studio supports only explicit private uploads to
the confirmed channel. It does not make videos public/unlisted, schedule them, monitor processing
or performance, or learn from retention data.

12. **Captions are burned-in or omitted.** The studio does not currently export SRT/VTT caption files or invoke YouTube dubbing/subtitle APIs.

13. **Costs may remain unknown.** Usage is counted where provider responses expose it, but exact currency totals require provider-reported prices or a maintained local rate card.

14. **Old projects can behave differently.** Projects created before later migrations may use staged reviews, lack publication clearance, or carry older artifacts. Behavior is determined by each project’s persisted policy, not only the current code version.

15. **Root exports are convenience copies.** SQLite and immutable versioned artifacts are the record of truth. Copying or editing a root export does not update the approved artifact.

## 15. Operator CLI reference

The CLI uses the same repository and workflow engine. Important commands are:

```text
studio create <brief.yaml>
studio topic <text> [--duration 12]
studio full <text> [--duration 55] [--rank-count N] [--image-budget N]
studio run <project-id>
studio status <project-id>
studio events <project-id>
studio approve <project-id> <gate>
studio reject <project-id> <gate>
studio revise <project-id> <message>
studio render <project-id>
studio repair <project-id> <version>
studio preview-review <project-id> <version>
studio refresh-preview <project-id> <version> <reason>
studio assets <project-id> <version>
studio recover-assets <project-id> <cached-response.json> <version>
studio recover-research <project-id> <evidence.json> <structured.json> <version>
studio music-import-batch <manifest.yaml>
studio polish <project-id> <manifest.yaml> <version>
studio doctor
studio worker [--once]
```

Recovery commands are operator tools, not Telegram features. They are designed to reuse already-paid cached responses after local validation or presentation fixes.

## 16. Example: one `/full` request from beginning to end

Suppose the user sends:

```text
/full 5 The most destructive earthquakes in recorded history
```

The system:

1. derives a project ID from the Telegram update;
2. creates a 55-second live, ranked, preview-policy project;
3. searches the web and records sourced candidate earthquakes;
4. requires an explicit destruction metric and ranking direction;
5. writes one continuous story that names ranks 5 through 1 in that order;
6. partitions the exact narration into shots;
7. synthesizes every line and measures its real duration;
8. reflows timing or rewrites up to two times if needed;
9. plans visual coverage, chooses matching local music, and generates up to the snapshotted image budget;
10. builds voice/music/SFX/ambience stems and a normalized mix;
11. renders the 9:16 MP4 and runs technical QA;
12. saves all composed frames and source illustrations in the project image gallery;
13. sends candidate hooks, chosen hook, complete script, and MP4 for review;
14. on preview approval, sends the title, description, usage summary, and thumbnail for publication clearance; and
15. on clearance approval, writes the seven-file root upload package and marks the project `COMPLETED`.

At no point does the program contain an earthquake-specific workflow branch. The topic-specific material exists only in research and project data.

## 17. Source map for this document

The most important implementation files are:

- `src/autonomous_studio/hitl/worker.py` — Telegram command routing, durable polling, and job scheduling
- `src/autonomous_studio/hitl/adapters.py` — review messages, media delivery, buttons, and sender validation
- `src/autonomous_studio/workflow/engine.py` — state machine, retries, timing repair, approvals, and revisions
- `src/autonomous_studio/workflow/assets.py` — asset planning, music selection, image preparation, and asset validation
- `src/autonomous_studio/workflow/publication.py` — YouTube metadata, usage report, release status, and root export
- `src/autonomous_studio/domain/models.py` — validated project, research, shot, audio, approval, and publication schemas
- `src/autonomous_studio/providers/openai.py` — live research, narrative, shot planning, and TTS adapters
- `src/autonomous_studio/providers/images.py` — bounded image generation and cache/journal behavior
- `src/autonomous_studio/providers/music_catalog.py` — local music matching
- `src/autonomous_studio/audio/timing.py` — measured timing and safe timeline reflow
- `src/autonomous_studio/audio/mixer.py` — stem creation, ducking, mixing, normalization, and clipping checks
- `src/autonomous_studio/rendering/renderer.py` — scene rendering, thumbnail gallery, MP4 assembly, and QA
- `src/autonomous_studio/storage/repository.py` — SQLite, events, locks, artifact hashing, and snapshots

This document should be updated when workflow semantics change; otherwise design discussions can accidentally describe a planned system rather than the bot users are actually running.
