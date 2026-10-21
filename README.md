# Autonomous Studio

A local-first, human-reviewed video-production agent for documentary Shorts and
chapter-based long-form videos. Supply a topic through Telegram or the CLI; the
studio develops the story, plans visuals, prepares narration and media, renders
a review video, and assembles a publication package. A new topic is project data,
not a new renderer or a new agent implementation.

Outputs are uploaded here regularly - https://www.youtube.com/@CryoKnowledge

This is an actively developed production tool, not an unattended publishing
service. Human approval gates, technical checks, rights records and durable
checkpoints are central to its design. Bugs and editorial limitations remain.

**Building a different video bot?** Start with the detailed
[video-production agent architecture handoff](docs/video-production-agent-architecture-handoff.md).
It separates reusable infrastructure from documentary-specific creative choices
and proposes a starting architecture for comedy and stick-figure animation.
Comedy production is not implemented here yet.

## Supported formats

| Format | Current behavior |
| --- | --- |
| Full Shorts | 1080×1920 vertical video, narration-led imagery and designed shots, music and optional burned-in captions. A 60-second editorial target can expand to 90 seconds automatically; longer results require runtime review, within the configured format limit. |
| Long-form | 1920×1080 horizontal video, question-first treatment, chapter script, speech-first visual beats, mixed imagery and semantic designed scenes. New projects start with a soft 210-second target and a 600-second expansion ceiling in their project policy. |
| Local prototypes | Deterministic mock providers and YAML-driven examples for offline development. Mock narration is timing tones, not intelligible speech. |

These are application policies, not recommendations about YouTube video length.
Existing projects retain their saved policies and model choices.

## Workflow

```text
Topic / brief → research and story → approved script and visual decisions
  → voice + media + motion → render and technical QA → human video review
  → publication package and separate clearance → explicit private upload
```

### Shorts

`/full <topic>` researches and writes one continuous story, then plans typed shots.
Speech is synthesized and measured before script approval. The script review
locks narration and prepared recordings. Visual assets are acquired after
approval; audio mixing and rendering lead to prototype approval, followed by a
separate publication review.

`/topic <topic>` is the faster prototype path, without the same full-production
script gate. Local YAML workflows retain their staged story/hero approvals;
those are not the default Telegram workflow.

### Long-form

`/longform <topic>` proposes story questions, gathers evidence and presents a
treatment and chapter outline. Structure approval leads to chapter writing,
continuity/editorial checks and script approval. Chapter and beat counts follow
the story: there is no universal five-chapter or 35-beat quota.

The visual planner first partitions approved speech, then chooses image-led or
designed scenes. For designed scenes it selects a registered family/treatment,
visible copy, graphic intent and spoken anchors. The registry currently has
20 families and 46 treatments; eligibility depends on available inputs.

Visual-plan approval starts production: measured speech, media, alignment,
illustration resolution, scene compilation and chapter rendering. The semantic
stage fills asset/composition details within approved visual contracts; it is not
a second scriptwriter. Chapters are assembled into one review MP4. Approval of
that video starts a separate publication review.

## Main capabilities

- SQLite state, ordered events, versioned artifacts, hashes, project locks and
  request journals support restart/resume without conversation history.
- Replaceable research, writing, image, TTS, rendering and HITL adapters; offline
  mocks for development and provider tests.
- Gemini 3.8 Flash TTS/Aoede by default for new live projects; OpenAI remains an
  option. `/ttsprovider` changes future projects, not existing recordings.
- Generated images, rights-compatible Commons photographs, code-rendered graphics,
  an SVG catalog and generated diagram cutouts.
- Speech-aware motion: Shorts use estimated word positions within measured audio;
  long-form uses transcribed word timestamps. These are different implementations.
- Music selection by mood first, trim/gain/fades/delays, narration ducking,
  normalization and clipping checks through a dedicated FFmpeg service.
- Typography safe areas, wrapping, conditional contrast treatment and render QA.
  Single-line `qualitative_comparison` and `metric_pair` card values are vertically
  centered; wrapped copy remains top-aligned.
- Separate title, description, thumbnail, source/rights manifests, release status
  and usage/cost records.

Research grounds factual content without requiring claim-by-claim narration.
Written sources do not by themselves justify document-themed scenes: visual
treatments follow what the narration communicates. Editorial quality, rights,
identity/context and pronunciation still require human review.

## Setup

The primary development environment is Windows with Python 3.12+. FFmpeg is found
on `PATH`, through `STUDIO_FFMPEG`, or via `imageio-ffmpeg`. Pillow handles
compositing; `resvg_py` rasterizes SVG assets.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\studio.exe --help
```

`pyproject.toml` defines dependencies. `requirements-lock.txt` records a known
Windows environment, rather than promising identical versions on every platform.
Model IDs in `.env.example` describe this deployment; availability must be checked
against the operator's accounts before paid calls.

Fill the ignored `.env` locally:

- `OPENAI_API_KEY`: research, writing, planning, images and long-form ASR.
- `GEMINI_API_KEY`: the default Gemini narration provider.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ALLOWED_USER_IDS`: Telegram.

Never put credentials in a brief or commit `.env`, `secrets/`, OAuth tokens or
provider caches. `.env.example` intentionally contains blank credential fields.

### Local music library

Download permitted music from YouTube Studio's Audio Library into `music/inbox/`.
Record exact title, artist, genre, mood and license/attribution evidence in a batch
YAML manifest, then import it:

```powershell
.\.venv\Scripts\studio.exe music-import-batch music/inbox/tracks.yaml
```

Import creates the local catalog and assets. Selection prioritizes mood, energy,
tempo and usable duration; track reuse is expected. Downloaded music, operator
catalogs and historical example MP3s are excluded from the source repository.
Mock demos generate their own audio. See the importer CLI help and the
[historical music setup notes](docs/history/readme-before-2026-10-21.md) for details.

## Running

```powershell
# Offline demo: no API keys or paid calls.
.\.venv\Scripts\python.exe scripts/smoke_demo.py

# Local mock workflow.
.\.venv\Scripts\studio.exe --root projects/demo create examples/earthquake-countdown.yaml
.\.venv\Scripts\studio.exe --root projects/demo run <project-id>
.\.venv\Scripts\studio.exe --root projects/demo approve <project-id> story

# One Telegram worker for the live project root.
.\.venv\Scripts\studio.exe --root projects/live worker
```

Use the same root when resuming. Start only one polling worker per bot; the worker
also takes an OS lock to prevent duplicates. Restart after code/environment
changes, without interrupting active production.

### Telegram interface

| Command | Purpose |
| --- | --- |
| `/full [rank-count] <topic>` | Full Short; explicit count only for an intended countdown. |
| `/topic <topic>` | Short prototype. |
| `/longform <topic>` | Chapter-based horizontal video. |
| `/settings` | Show future-project preferences. |
| `/ttsprovider [gemini\|openai\|reset]` | Shared future-project TTS provider; `/longformtts` remains an alias. |
| `/imagebudget [1–18\|reset]` | Shorts generation budget. |
| `/longformimagebudget [4–30\|reset]` | Long-form generation budget; default 30. |
| `/closedcaption [yes\|no]` | Optional burned-in captions; `/closed-caption` is an alias. |
| `/status <project-id>` | Durable project status. |
| `/revise <project-id> <version> <feedback>` | Version-bound creative feedback. |
| `/retry <project-id>` | Resume an eligible failed stage, not repair an invalid draft automatically. |
| `/repair <project-id> <version>` | Existing bounded narration-timing repair path. |
| `/uploadyoutubeshorts <project-id>` | Explicit private upload of a completed, cleared package. |
| `/refreshyoutubeauth` | Refresh saved access token and verify channel; revoked grants need local consent. |

Approvals use review buttons or CLI commands and bind to a project, gate, version
and artifact set. Old buttons cannot approve newly revised material.

## Outputs and private YouTube upload

Projects live under `projects/live/<project-id>/`, with versioned artifacts,
provider caches, separately saved images and review material. Once video and
publication clearance are approved, stable root exports include:

```text
final-video.mp4
youtube-title.txt
youtube-description.txt
youtube-metadata.json
selected-thumbnail.png
release-status.json
usage-report.json
```

Source/rights manifests remain traceable artifacts. Pending packages do not
overwrite the last cleared release. Oversized long-form reviews are copied
unchanged to the project root for manual review instead of being compressed to
fit Telegram's limit.

Authorize desktop OAuth for the intended channel before uploading:

```powershell
.\.venv\Scripts\studio.exe --root projects/live youtube-auth https://www.youtube.com/@YOUR_CHANNEL
.\.venv\Scripts\studio.exe --root projects/live upload-youtube-short <project-id>
```

The uploader checks channel identity and package hashes, sends the finalized
publication-selected thumbnail and writes a receipt. It requests private
visibility, not-made-for-kids, the saved narration language (English by default),
no paid promotion and synthetic-media disclosure. It never makes a video public.
Thumbnail failures can retry only the thumbnail; uncertain video uploads are not
automatically replayed. See [upload details](docs/youtube-upload-defaults.md).
The historical command name is not a promise of a separate long-form upload UI;
use the completed-package operations exposed by the current build.

## Repository map

```text
src/autonomous_studio/
  domain/       Project, approval, timing, motion and scene contracts
  workflow/     Durable coordination, script locks, scene compilation, publication
  providers/    Live/mocked research, writing, media, voice and upload adapters
  storage/      SQLite, events, hashes, immutable artifacts and locks
  rendering/    Shorts editorial layouts, long-form visual families and FFmpeg
  audio/        Timing, alignment and mixing
  hitl/         Telegram/local reviews, commands and polling worker
  cli/          Typer operator interface
examples/       Topic fixtures and declarative design studies
tests/          Offline contracts, provider fixtures and real-render regressions
tools/          Design studies, SVG importers and launcher source
scripts/        Demos, diagnostics, previews and targeted operator repairs
docs/           Architecture handoff, operating notes and dated development history
```

Start with the [architecture handoff](docs/video-production-agent-architecture-handoff.md),
[visual registry](docs/visual-family-registry.md),
[TTS settings](docs/longform-gemini-tts.md) and
[recent Shorts notes](docs/shorts-card-cues-and-shared-tts.md).
Historical roadmaps may contain superseded behavior; code and saved project
policy win when they disagree. The previous README is preserved under `docs/history/`.

## Testing and maintenance

```powershell
New-Item -ItemType Directory -Force out/test-runs | Out-Null
.\.venv\Scripts\python.exe -m pytest tests/test_shorts_card_cues.py tests/test_gemini_tts.py tests/test_youtube.py -q --basetemp out/test-runs/readme-smoke
.\.venv\Scripts\python.exe -m pytest -q
```

The full suite includes actual FFmpeg rendering and takes time. Provider tests
use mocked HTTP, not live paid APIs. If Windows temp permissions interfere, use
a fresh dedicated `--basetemp`; pytest can delete its contents, so never select
a project/data directory as the test temp root.

On live failure, inspect the saved request/output and identify the upstream cause.
Fix topic-independent studio defects with regression tests before resuming;
preserve approved narration and successful media. Timeouts, moderation rejections,
bad drafts and uncertain uploads are different failures, not interchangeable
reasons for a blind retry. See [AGENTS.md](AGENTS.md).

## Boundaries and licenses

- No promise of viral reach, perfect factual accuracy, human performance,
  error-free visuals, complete archival coverage or automatic rights clearance.
- No automatic public publishing or analytics-driven global learning from feedback.
  Only explicitly persisted settings and project feedback influence later work.
- Budgets are ceilings, not spending targets. Cost stays unknown without reliable
  provider usage/rate information.
- Live caches/journals are not disposable temp files: they preserve evidence and
  prevent duplicate expenditure.

The GitHub repository's existing [CC0 license](LICENSE) is retained. Fonts, icons,
maps and other third-party assets retain their separate licenses/notices. CC0
does not override those terms or grant rights to all generated/acquired media.
