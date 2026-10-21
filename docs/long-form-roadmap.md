# Long-Form Studio — Research and Deferred Roadmap

Status: **LF v0.4 implemented; live acceptance awaiting visual-plan approval**  
Recorded: 2026-09-21; updated 2026-09-23  
Implementation status: **Phase 4 production pipeline implemented on 2026-09-23**

This document preserves the research and architectural recommendations for extending Autonomous Studio from YouTube Shorts to long-form videos. It should be reviewed and updated with lessons from continued Shorts testing before each subsequent phase begins.

For the deferred long-form planner, zero-repair, model-routing, and story-first
writing work agreed on 2026-10-07, see
[Long-form planner and storytelling refactor plan](long-form-planner-and-story-refactor-plan.md).

## Phase 1 implementation record

LF v0.1 now provides:

- A shared format-profile registry for Short prototype, full Short and long-form pilot constraints.
- Backward-compatible loading of existing Short YAML files and persisted Short projects.
- Versioned `LongFormBrief` and `LongFormSpec` models.
- Chapter, narration-block, visual-beat, duration-policy and engagement-ending schemas.
- A 150–240 second `longform_pilot` profile targeting 1920×1080, 16:9 output.
- Generic CLI creation and status output for long-form briefs.
- Durable persistence and an idempotent `LONGFORM_FOUNDATION_READY` checkpoint.
- Burned-in captions off by default for long-form projects, persisted per project.
- A topic-data example under `examples/longform-pilot.yaml`.

Phase 1 deliberately does not call research, directing, image, TTS or rendering providers for long-form projects. Those capabilities begin in subsequent phases after their approval gates and chapter-level recovery semantics are implemented.

## Phase 2 implementation record

LF v0.2 now provides:

- `/longform <topic>` and `studio longform <topic>` entry points for a 210-second pilot.
- A durable research plan, web-grounded research dossier and chapter evidence coverage artifacts.
- A treatment containing candidate hooks, one selected opening, a viewer promise, thesis,
  3–5 chapter outline, conclusion and topic-specific engagement ending.
- A Telegram treatment approval before the complete narration is written.
- Chapter-by-chapter script generation followed by deterministic and model-assisted global
  continuity checks for hook fidelity, evidence coverage, duration, repetition, transitions and
  the approved ending.
- A second Telegram approval containing the complete continuity-validated script before visual
  planning or paid asset generation.
- Checkpoint-aware treatment and script revisions: research is reused, treatment revisions rebuild
  the outline, and script revisions preserve the approved treatment while replacing narration.
- OpenAI Responses API background jobs with strict structured outputs for live research,
  treatments, chapter scripts and continuity audits.
- Immediate Telegram acknowledgements after both treatment and script approval.
- Deterministic local providers and tests for restart recovery, revision behavior, Telegram
  settings, caption defaults and provider request contracts.

LF v0.2 intentionally stops at `LONGFORM_NARRATIVE_READY`. It does not acquire images, synthesize
speech or render a video; those activities begin only after visual planning and rights review are
implemented in LF v0.3.

## Phase 2.5 implementation record

LF v0.2.5 replaces the original hard narration target with an approval-led elastic-duration
policy:

- The 210-second pilot duration remains the editorial target and 240 seconds remains the normal
  planning range.
- Narration above 95% of the target is a warning rather than an automatic failure. The configured
  300-second pilot ceiling is a planning and cost checkpoint, not a YouTube recommendation.
- The continuity audit must reject repetition, filler and unnecessary digressions; duration alone
  is not a defect when the additional material advances the approved story.
- Script review shows the original target, estimated narration and proposed finished runtime.
- Approving an expanded script explicitly adopts the proposed runtime and proportionally expands
  chapter timelines before visual planning.
- After TTS, measured narration that exceeds the approved timeline triggers a durable runtime
  proposal before image acquisition. Telegram offers **Keep script · extend** with the proposed
  finished length, **Trim script** followed by a new script review, or **Stop**. The keep choice
  retimes the chapters without deleting or accelerating speech. A 1,200-second format limit
  remains as an operational safety bound.
- Projects stopped by LF v0.2's former `target × 0.95` failure can be retried under the new policy
  without inventing human trimming feedback.

Measured TTS remains authoritative once narration synthesis is implemented. Phase 2.5 therefore
approves an editorial runtime estimate; later measured timing may refine it without silently
cutting or accelerating speech.

## Phase 3 implementation record

LF v0.3 now provides:

- A chapter-aware mixed-media visual planner that produces 20–35 meaning-led beats against the
  approved narration without rewriting it.
- Separate authentic-image, generated-image and code-rendered asset requests, with generated and
  authentic-media budgets enforced before acquisition.
- A default long-form generated-image planning budget of 14, independently configurable for
  future projects through `/longformimagebudget <4-24>` without changing the Shorts budget.
- A Wikimedia Commons discovery adapter restricted to compatible public-domain, CC0 and CC BY
  candidates, plus deterministic offline fixtures for tests.
- Exact per-candidate source page, creator, licence and attribution records, while retaining a
  mandatory human context/personality-rights review for authentic media.
- Generated-image fallback when no compatible authentic candidate is found; the fallback counts
  against the generated-image budget and is visible in the plan.
- A pre-spend cost estimate that distinguishes known zero-cost planning work from provider prices
  that are not configured.
- Durable visual-plan attempts, bounded internal repair, restart recovery, revision semantics and
  a Telegram visual-plan approval containing contact sheets.
- Low-resolution authentic-media previews for live contact sheets only. No paid image generation,
  production-media acquisition, TTS or rendering starts in this phase.

Approval leaves the project at `LONGFORM_VISUAL_PLAN_READY`. LF v0.4 consumes that approved,
rights-aware plan to acquire production assets, synthesize audio and render chapter intermediates.

## Phase 4 implementation record

LF v0.4 now provides:

- Durable music selection, per-block TTS preparation and measured narration-window validation.
- Approved generated-image acquisition, exact-source Commons acquisition and deterministic
  code-rendered/fixture images. Generated images are retained separately under `images/vNNN/`.
- Content-addressed paid-request caches and reservation journals; ambiguous interrupted requests
  stop for inspection rather than silently repeating a potentially charged call.
- 1920×1080 FFmpeg chapter renders with voice-priority music ducking, loudness normalization,
  clipping checks and optional burned-in captions. Captions are off unless the project's
  persisted setting enables them.
- Chapter input hashes and durable output references so unchanged chapters can be reused.
- Validated concatenated review MP4 and a Telegram review gate. Files over 49 MB are copied
  unchanged to `review-video-vNNN.mp4` in the project root for manual review.
- A project-root `final-video.mp4` master after prototype approval. Neither the saved visual
  plan nor the video preview is auto-approved by deployment.
- Offline tests for restart recovery, selective chapter rerender, caption on/off rendering,
  music-plus-voice mixing and oversized review messaging.

The current live project's visual plan still requires human approval. LF v0.4 does not add
long-form YouTube metadata, upload or publication clearance; those remain LF v0.5 work.

## Initial recommendation

Begin with a 3–4 minute, chapter-based, 16:9 documentary/explainer rather than immediately targeting eight minutes.

The initial production profile should be:

- Duration policy: 150 seconds minimum, 210 seconds target, 240 seconds maximum.
- Resolution: 1920×1080.
- Aspect ratio: 16:9.
- Frame rate: 24 fps unless source footage requires another rate.
- Chapters: 3–5.
- Narration: approximately 420–600 words, with measured TTS duration authoritative.
- Visual beats: approximately 20–35.
- Newly generated images: initially 8–12, controlled by a separate long-form budget.
- Final output: H.264/AAC, SDR BT.709, fast-start MP4.
- Review output: a configured review artifact; send it through Telegram only when it already fits, otherwise place the unchanged file in the project root.
- Burned-in captions: controlled by the persisted project setting and off by default.

Move to a 6–8 minute profile only after several shorter pilots establish stable production cost, narrative quality and audience retention.

YouTube does not prescribe one ideal video length. The subject should determine the duration, without padding. Longer videos benefit from absolute watch time, but viewer satisfaction and retention remain the deciding signals.

## Visual strategy

A still-image-led three-minute video can work, but the default should not be a regular slideshow. Use a mixed-media documentary grammar:

- Authentic photographs, places, people, documents and archival material when suitable rights are available.
- Generated editorial illustrations where authentic imagery does not exist.
- Maps, timelines, comparisons, diagrams and charts rendered from data.
- Meaningful camera movement and signaling: detail crops, camera pushes, progressive highlights, annotations and parallax.
- Short motion clips or screen recordings only when motion improves the explanation.
- Visual evidence aligned to the exact narration being spoken.
- Chapter-aware music, ambience and sound design.

Initial planning heuristic:

- Develop or change the visual idea approximately every 5–12 seconds, based on meaning rather than a rigid timer.
- Reuse strong assets through different crops, annotations and framing instead of generating one new image for every beat.
- Do not use animation merely to create movement. Direct attention to the relevant evidence or relationship.
- Avoid repetitive templates and minimally transformed slideshows.

## Architecture recommendation

Do not stretch the existing Short schema with format-specific conditionals. Add format profiles over a shared studio core:

```text
                         STUDIO CORE
 Research and claims     Durable jobs and events
 Provider contracts      Artifact versions and caching
 Approvals               Rights and provenance
 Audio and music         Usage and cost reporting
 Publishing and upload
                              |
                 +------------+------------+
                 |                         |
          SHORTS PROFILE            LONG-FORM PROFILE
          9:16 / <=60 sec           16:9 / chapter-based
          shot sequence             chapter -> visual beats
          loop ending               resolved conclusion
```

### Reuse from Shorts

- SQLite state and event history.
- Durable restart and resume behaviour.
- Artifact hashing and versioning.
- Provider boundaries.
- Claim and source records.
- OpenAI background-response recovery.
- Approval and feedback records.
- Image and TTS caching.
- Music catalogue and licensing metadata.
- Audio ducking, normalization and clipping checks.
- Publication clearance, usage reporting and private YouTube upload.

### Extend

- Project format and duration models.
- Research depth and source coverage by chapter.
- Asset planning and rights records.
- Narration structure and global continuity checks.
- Telegram progress and review presentation.
- Chapter-based audio assembly.

### Replace for long-form

- The one-shot Short director prompt.
- The flat shot list as the top-level story structure.
- Per-frame Python/Pillow rendering for an entire video.
- Full-project rerenders after a local change.
- Sending the full-resolution master through Telegram.

## Proposed domain model

Introduce a versioned `LongFormSpec` sharing common project interfaces while owning its validation rules.

```text
LongFormSpec
  format: youtube_longform
  duration_policy
    minimum_seconds
    target_seconds
    maximum_seconds
  narrative
    viewer_promise
    central_question
    thesis
    hook
    chapter_arc
    conclusion
    next_video_bridge
  chapters[]
    purpose
    question
    reveal
    transition
    narration_blocks[]
    evidence[]
    word_budget
    visual_beats[]
    audio_direction
  visual_beats[]
    narration_reference
    asset_reference
    framing/treatment
    overlays/signals
    duration
    source and rights
```

The narration sentence and visual shot must not be treated as a one-to-one relationship. A coherent paragraph may cover several visual beats, and one evidence-rich visual may develop across multiple narration lines.

## Narrative process

Avoid generating the complete project in one model response. Use these durable stages:

1. Research dossier and evidence ledger.
2. Treatment containing the angle, central question, viewer promise and payoff.
3. Chapter outline with purpose, reveal, transition and word budget.
4. Outline approval.
5. Chapter scripts generated against a shared story bible.
6. Global continuity pass for repetition, terminology, transitions, open loops and payoff.
7. Measured TTS and bounded timing repair.
8. Script approval.
9. Visual direction based on the approved script.

The opening 15–30 seconds should fulfil the promise made by the title and thumbnail. Unlike a Short, the ending normally should resolve the central question rather than force a seamless loop. It can naturally bridge to a related video or end screen.

## Engagement ending and calls to action

YouTube permits creators to ask viewers to subscribe, like, share or comment. It also recommends specific questions as a way to build useful discussion, and its end-screen feature is designed for the final 5–20 seconds of a video.

The studio should therefore include an engagement ending, but it must be part of the story rather than a generic interruption. Recommended policy:

- Reserve approximately 8–15 seconds at the end for the conclusion, engagement prompt and end-screen layout.
- Complete the video's central promise before or during this section; never withhold the promised answer merely to solicit engagement.
- Use one concise, value-linked subscription reason, such as: “If you enjoy evidence-led stories like this, subscribe for the next one.”
- Ask one specific, topic-relevant question rather than “What do you think?” The question may address a genuine uncertainty, interpretation, trade-off or next topic.
- Include a brief combined like-and-share prompt tied to the value received, for example: “If this made the idea clearer, like the video and share it with someone who would enjoy it.”
- Avoid presenting subscribe, like, share and comment as four disconnected commands. Combine the secondary actions into one natural sentence.
- Keep one primary action per ending. For this channel, the primary action should normally be the topic-specific comment question; subscription, like and share should form a short secondary prompt.
- Keep important visuals and text out of the areas reserved for YouTube end-screen overlays.
- Generate a suggested pinned comment containing the same specific question, so the conversation can continue without repeating a long prompt in the narration.
- Record end-screen element click rate, comments, subscriptions and retention through the ending as separate evaluation signals.

The long-form domain model should add:

```text
engagement_ending
  conclusion_bridge
  subscribe_reason
  comment_question
  like_and_share_prompt
  suggested_pinned_comment
  end_screen_target
  planned_duration_seconds
```

The director must validate that the comment question genuinely follows from the subject and has not been fabricated solely to create an artificial open loop.

## Proposed workflow

```text
CREATED
  -> RESEARCHING
  -> TREATMENT_APPROVAL
  -> SCRIPTING
  -> SCRIPT_APPROVAL
  -> VISUAL_PLANNING
  -> VISUAL_PLAN_APPROVAL
  -> ASSETS_PREPARING
  -> CHAPTER_RENDERING
  -> REVIEW_ARTIFACT_READY
  -> PROTOTYPE_APPROVAL
  -> MASTER_RENDERING
  -> PUBLICATION_CLEARANCE
  -> COMPLETED
```

For the first three pilots, retain four user-facing review points:

1. Treatment and chapter outline.
2. Complete narration script.
3. Visual plan and estimated paid-asset cost.
4. Finished review artifact.

The visual-plan gate can later become conditional when estimated cost remains within a user-approved standing budget.

## Rendering and audio

Render by chapter rather than as one monolithic job:

- Store each chapter's input and output hashes.
- Rerender only changed chapters.
- Concatenate validated chapter intermediates.
- Use FFmpeg-native motion and overlays where practical.
- Create a chapter-complete review artifact before the final publication master.
- Render the publication master only after review-artifact approval.
- Mix and validate chapter audio before creating the master mix.
- Avoid writing thousands of individual image frames.

TTS should use coherent paragraph or scene blocks rather than one request per visual beat. This should improve prosody and reduce provider calls. Multiple visual beats can be aligned to one narration block.

## Telegram design

Telegram should be the control plane, not the master-media repository. It should carry:

- Treatment and outline summaries.
- Script documents.
- Contact sheets and hero-frame reviews.
- Chapter progress.
- Cost estimates and approvals.
- Short audio samples.
- A review video when it already fits Telegram's file limit.
- Final completion and YouTube upload status.

The regular Telegram Bot API currently limits uploaded files to 50 MB. The studio must not lower resolution, reduce bitrate or otherwise recompress a review video solely to fit that limit.

Review-delivery policy:

1. Render the review artifact using the project's configured review profile.
2. If the resulting file is within Telegram's supported limit, send it normally.
3. If it exceeds the limit, atomically copy it to the project root as `review-video-vNN.mp4`.
4. Persist its path, version, size and hash in project state.
5. Send a Telegram message stating that the review is ready locally, including the project ID, version, filename and file size, with the normal approve/revise/reject actions.
6. Do not mark Telegram delivery failure as a video-production failure.
7. A later storage-delivery adapter may upload the same immutable artifact to Google Drive and place its review link in Telegram; that integration is explicitly deferred.

The approved 1080p publication master should continue to be placed in the project root and uploaded directly to YouTube as private.

## Cost and reliability

- Maintain a separate long-form budget rather than reusing the Shorts image budget.
- Estimate provider and rendering cost before paid asset generation.
- Generate low-cost drafts before final-quality images.
- Generate final-quality assets only after visual-plan approval.
- Prefer suitably licensed authentic material when appropriate.
- Cache narration and images by content hash.
- Regenerate only changed chapters.
- Do not add generative video in the first release.
- Limit concurrent image and TTS calls.
- Reserve budget atomically before provider calls.
- Persist every provider job ID and polling cursor.
- Retry at chapter/task level rather than restarting a project.
- Add a cancellation path that stops before the next paid operation.
- Estimate disk requirements and clean disposable intermediates after completion.

OpenAI implementation guidance:

- Retain background Responses for long-running interactive work.
- Structure prompts with stable instructions, schemas and shared research first so prompt caching can reuse the prefix.
- Consider an optional economy mode using the Batch API for non-interactive work such as shot plans, image generation and internal QA.
- Do not use Batch for approval-critical work where the user is actively waiting.

## Authentic-media acquisition

Add a real-media provider alongside generated-image providers. Its results should record:

- Original URL and creator.
- Licence and attribution requirements.
- Downloaded original and approved derivatives.
- Subject, date and location metadata.
- Whether modification is permitted.
- Publication-clearance status.

Initially permit only material with clearly compatible publication rights, such as public-domain collections, compatible Creative Commons licences, and official government or scientific sources. Unknown rights must block publication. Realistic AI reconstructions of people, places or events should be evaluated for YouTube altered-content disclosure.

## Deferred implementation sequence, outcomes and estimates

The estimates below assume focused implementation against the existing Shorts codebase, working providers and FFmpeg setup. They are engineering estimates rather than calendar commitments; external API latency, newly discovered Shorts regressions and the complexity of real-media licensing can change them.

### LF v0.1 — Foundation (completed 2026-09-22)

- Format-profile architecture.
- `LongFormSpec`, chapters and visual beats.
- 3–4 minute, 16:9 projects.
- No regression to Shorts.

Outcome: the CLI and durable workflow can create, validate, persist and resume a long-form project without altering Shorts behaviour. No polished video is expected yet.

Estimated effort: 1–2 focused engineering days.

### LF v0.2 — Research and directing (completed 2026-09-22)

- Research plan and chapter evidence coverage.
- Treatment and outline generation.
- Chapter-based scripting.
- Global continuity checks.
- Outline and script approvals.

Outcome: Telegram can take a long-form topic through researched treatment, chapter outline and complete coherent narration, with explicit approvals before asset spending.

Estimated effort: 1–2 focused engineering days after LF v0.1.

### LF v0.2.5 — Elastic narrative duration (completed 2026-09-22)

- Soft narration target and explicit projected runtime.
- Quality-led duration assessment and hard cost ceiling.
- Runtime adoption at script approval.
- Legacy duration-failure recovery.

Outcome: a coherent long-form script is no longer rejected solely for exceeding the initial
3:30 target. The reviewer sees and approves the resulting runtime before any visual spending.

### LF v0.3 — Visual planning and rights (completed 2026-09-22)

- Mixed-media visual planner.
- Authentic-media provider boundary.
- Rights and attribution manifest.
- Cost estimate and contact-sheet approval.

Outcome: the system can produce an approved mixed-media storyboard and asset plan, including generated visuals, code-rendered visuals and rights-aware authentic-media candidates.

Estimated effort: 2–3 focused engineering days. A provider boundary and manually supplied/public-domain fixtures can be included in the MVP; broad unattended web acquisition may require additional hardening.

### LF v0.4 — Scalable rendering

- Chapter rendering and audio mixing.
- Selective rerendering.
- Telegram-or-project-root review delivery based on resulting file size.
- 1080p master.
- Storage cleanup and restart recovery.
- Caption rendering controlled by the persisted project setting, with burned-in captions off by default.

Outcome: the first end-to-end MVP—a 3–4 minute, 16:9 audiovisual video with chapter recovery, an engagement ending and a review artifact—is available. Oversized review files remain unchanged in the project root and Telegram provides the review notification and controls.

Estimated effort: 2–4 focused engineering days after LF v0.3, including render verification.

### LF v0.5 — Publication and evaluation

- Long-form metadata, chapters and thumbnail package.
- Private YouTube upload.
- Phone and television review.
- Manual retention review.
- Evidence-based decision on enabling 6–8 minute projects.

Outcome: an approved long-form master has a complete publication package, private YouTube upload, reserved end-screen layout, specific comment prompt and evaluation checklist.

Estimated effort: 1–2 focused engineering days, followed by human review and post-publication analytics collection.

### When the first MVP should be visible

There are two useful milestones:

- **Narrative MVP after LF v0.2:** approximately 2–4 focused engineering days. It will demonstrate research, chapter structure, coherent script generation, the engagement ending and Telegram approvals, but not the final scalable renderer.
- **End-to-end video MVP after LF v0.4:** approximately 6–11 focused engineering days in total. It will render a 3–4 minute video, preserve restart safety, deliver the review locally or through Telegram according to file size, and support selective chapter rerendering.

The full LF v0.5 production candidate is estimated at approximately 7–13 focused engineering days, excluding time spent waiting for user approvals and audience analytics. Continued Shorts bug collection should happen before starting this clock because shared-core fixes may reduce long-form rework.

## Initial acceptance criteria

- Coherent 180–240 second non-countdown video.
- 1920×1080, 16:9 video with validated audio.
- Approved treatment, script, visual plan and review artifact.
- 3–5 chapters and 20–35 purposeful visual beats.
- Factual narration mapped to evidence.
- No unresolved asset licensing.
- Project stays within configured provider budgets.
- Restart does not repeat completed paid work.
- A changed chapter can be rerendered independently.
- Review delivery follows the size policy: send through Telegram when supported; otherwise preserve the unchanged artifact in the project root and notify the reviewer.
- Conclusion resolves the viewer promise and includes a concise engagement ending.
- Engagement ending contains a topic-specific comment question plus a short, value-linked subscribe, like and share prompt.
- Video composition reserves a safe 5–20 second end-screen region without covering important content.
- Master and publication package are easy to locate in the project root.
- Private upload to the configured YouTube channel.
- Existing Shorts behaviour remains unchanged.
- Automated rendering tests cover both caption modes: captions are absent when disabled and correctly timed/readable when enabled.
- Caption-mode tests cover resumed projects and selective chapter rerenders so the persisted setting cannot be lost.

## Research references

- YouTube video-length guidance: https://support.google.com/youtube/answer/16559651?hl=en
- YouTube audience retention: https://support.google.com/youtube/answer/9314415?hl=en
- YouTube performance FAQ: https://support.google.com/youtube/answer/141805?hl=en
- YouTube formatting: https://support.google.com/youtube/answer/4603579?hl=en
- YouTube encoding: https://support.google.com/youtube/answer/1722171?hl=en
- YouTube monetization and inauthentic content: https://support.google.com/youtube/answer/1311392?hl=en
- YouTube altered-content disclosure: https://support.google.com/youtube/answer/14328491?hl=en
- YouTube fake-engagement policy and permitted calls to action: https://support.google.com/youtube/answer/3399767?hl=en-GB
- YouTube end screens and best practices: https://support.google.com/youtube/answer/6388789?hl=en
- YouTube community-building through comments: https://support.google.com/youtube/answer/11913117?hl=en
- OpenAI background mode: https://developers.openai.com/api/docs/guides/background
- OpenAI prompt caching: https://developers.openai.com/api/docs/guides/prompt-caching
- OpenAI Batch API: https://developers.openai.com/api/docs/guides/batch
- OpenAI image generation: https://developers.openai.com/api/docs/guides/image-generation
- Telegram Bot API: https://core.telegram.org/bots/api
- Educational-video study: https://link.springer.com/article/10.1007/s11423-025-10572-0

## First live acceptance run

The saved project `154080f0be98` is at `LONGFORM_VISUAL_PLAN_READY` with an approved v2 plan and
a cap of 14 generated-image requests. The Telegram worker is currently stopped. Starting it will
begin live media acquisition and TTS under that approval. Review the plan and expected provider
spend before starting; no live API spend was performed as part of the code implementation.

After production, inspect the review MP4 for factual and visual accuracy, narration, caption
preference, music rights and authentic-media context. Approval then exposes the root master.
