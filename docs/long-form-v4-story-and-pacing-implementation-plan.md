# Long-form v4: story, pacing and image-led scenes — implementation plan

**Status (2026-10-19):** Production code and offline regressions implemented for both workstreams. Existing approvals and the Sun v3 project remain unchanged. A live, versioned Sun replan and human review are still pending; no paid generation was run for this change.

**Story-question discovery extension:** The existing Luna research-plan request now proposes two or three testable audience questions before evidence gathering, including what evidence might contradict each one. Research investigates all candidates without choosing a winner. The existing Sol treatment request then selects one candidate on the gathered evidence, records its supporting claim IDs, rationale and limitation, and shows that choice at the structure gate. Missing or ungrounded choices fail validation instead of turning an attractive premise into an unsupported script. Older saved plans without candidates remain loadable; they retain the prior evidence-first treatment behavior unless explicitly replanned. This adds no model call. The Sun project has not been resumed.

## Outcome sought

The story should have as many chapters as its distinct turns in understanding require—even one or two—not a prescribed three-to-five. The narration should explain unfamiliar ideas clearly, let consequential points land, and keep moving without a continuous-commentary feel. Script reviews should show the story and spoken words, **not a routine estimated spoken runtime**. The studio will still calculate durations internally to avoid clipping or silently breaching the existing production ceiling. Once the script is approved, visual planning should choose an image whenever one truthful focal scene communicates the spoken idea better than a diagram, regardless of the number of spoken-role groups.

This plan combines two v4 workstreams: story length/pacing and image-led selection by visual fitness. Other v4 visual proposals remain separate decisions.

## Workstream A: story structure and pacing

### Current behavior to change

| Area | Current behavior | Proposed behavior |
| --- | --- | --- |
| Treatment | The prompt requests 3–5 chapters; draft and persisted schemas enforce that range. | Ask for the fewest chapters that represent genuine changes in the viewer's understanding. Allow one or more; do not replace 3–5 with another arbitrary chapter-count ceiling. |
| Chapter shape | Each chapter has a purpose, question, reveal, transition, duration and evidence IDs. | Keep those useful fields. Require each chapter to make a distinct advance; merge a chapter that only recaps, changes labels, or repeats a qualification. A single-chapter story must still have a hook, development and payoff. |
| Script | The writer produces 2–8 blocks per chapter and is asked to explain and bridge, but has no explicit, durable landing-point marker. | Write in spoken, newcomer-friendly language. End selected blocks at genuine conceptual landings or turns; include a small editorial landing cue on those blocks. Do not split a thought merely to create another block or chapter. Review whether the fixed block-count range also needs relaxing so one- and two-chapter stories remain practical. |
| Edit | The narration scheduler adds a fixed 0.12-second gap between blocks. | Use a short ordinary join by default and a bounded pause only at marked landings; allow a slightly larger chapter-turn pause when earned. Keep an active or purposeful visual during any pause. |
| Voice | TTS uses `gpt-4o-mini-tts`/Coral at `speed=1.0`; consistent-delivery mode does not pass the per-block direction into the request. | Keep model, voice and global speed unchanged for this phase. Test a narrowly bounded delivery instruction or block direction only if script structure and edited pauses do not solve the perceived hurry; do not introduce a blanket slower voice. |

### Implementation sequence (steps 1–3 done; live proof in step 4 pending)

1. **Treatment and schema.** Remove the 3–5 wording and chapter-count constraints from the treatment draft, persisted `LongFormSpec`, and related validation. Keep contiguous chapter order, nonempty purpose/reveal, evidence integrity and chapter durations summing to the internally approved target. Add a treatment-writing instruction to choose a chapter boundary only when the viewer's understanding, question, or stakes genuinely turns. Do not create a standalone recap to satisfy a count. Review any downstream assumptions about “final chapter” or fixed chapter indices.

2. **Script and landing contract.** In the existing chapter-writing request, ask for clear explanations before new jargon, varied sentence rhythm, and an answer or insight at each chapter's landing. Add one simple, optional `landing_after` enum to narration blocks (ordinary / idea_landing / chapter_turn), defaulting to ordinary for saved projects. Map that cue to small, bounded edit gaps in code; do not ask the model to choose arbitrary seconds. The writer places a marked landing only after a complete spoken thought. A chapter transition should advance the story, not re-ask the preceding chapter's question. Keep the existing factual guardrails and essential-claim coverage, but do not turn narration into a claim-by-claim recital.

3. **Timing and audio propagation.** Replace the fixed inter-block gap in `narration_windows()` with the cue-driven value. Have measured-audio retiming, chapter boundaries, reveal timestamps, captions (on and off), music ducking and the final mix use the same scheduled windows. No speech is cut or accelerated to make room. If the internally measured total exceeds the existing policy, use the established duration decision path rather than hiding the overrun or removing the landing points automatically. Do not add a routine runtime estimate to Telegram story/script reviews.

4. **Proof before production use.** Run offline fixtures for one, two, five and more-than-five chapters; verify schema loading, contiguous order, duration sums, script generation handoffs, final-chapter handling and no empty/recap-only chapter. Add audio-timeline tests for ordinary joins versus marked landings, no overlap/clipping, reveal alignment and captions on/off. Use a new versioned Sun treatment/script candidate for human review against v3, without rewriting or auto-approving its existing decisions. Proceed to TTS/render only after the normal script approval gate.

### Acceptance criteria

- A one- or two-chapter outline is valid when it tells the story well; five is also valid when each chapter earns its place.
- The script advances a coherent question-to-payoff arc, explains unfamiliar concepts before relying on them, and contains marked landings only where a viewer benefits from a moment to absorb the idea.
- Intentional pauses survive measured TTS timing into video, audio mix and optional captions; they do not create dead screens or shift visual reveals away from speech.
- Script approval remains a human decision. The Telegram review does not show a routine estimated spoken runtime, while internal safety checks and the established ceiling remain active.
- Previously approved projects continue to load and retain their approvals; the new behavior applies to new projects or an explicitly versioned replan.

## Workstream B: choose image-led scenes by visual fitness

### What the current planner actually does

The current two-call visual planner first partitions approved speech into beats and then assigns each beat a visual mode and treatment. In `providers/longform_visuals.py`, the compiled menu sets `image_allowed` only when the beat has exactly **one** spoken-role group (except the fixed closing beat). The second call is told it may choose an image only if that flag is true. Another instruction describes generated imagery partly as what to choose *when no registered treatment fits*. This makes a feasible designed treatment easier to pick even when one image would tell the story more vividly. The fixed image-led opening is an exception, not evidence that later beats are assessed equally.

An image beat currently supports at most **two speech-timed overlay cues**. That is a renderer capacity limit, not a reason to prohibit an image merely because narration has multiple role groups. The policy permits up to 30 newly generated image requests and 12 authentic-media candidates by default; those are ceilings, not targets. An authentic search miss can take the existing safe generated-image fallback if budget remains.

### Selection rule to implement

After the beat boundary is fixed, consider both modes side by side:

- **Image-led** when one specific, honest focal scene can convey the principal subject, event, place, or relationship through the whole beat. Multiple spoken roles can be carried by changes of emphasis, camera movement or up to two sparse, speech-aligned overlays. The image must not imply an unspoken event, reconstruct documentary evidence as authentic, or hide an essential relation.
- **Designed treatment** when comprehension relies on explicit sequence, measurable comparison, geometry, changing states, chronology, a conditional branch, or another relation that one image would leave ambiguous. A treatment being technically eligible does not make it the better choice.
- **Neither fits** when the beat actually contains separate visual ideas or would need more than two essential labels. Return a beat-boundary diagnostic for review/repartition through the existing process; do not squeeze it into a collage, invent labels, exceed overlay capacity, or silently select a weak treatment. The closing action beat keeps its dedicated treatment.

Use a *modest image tie-break* only when both modes explain the speech equally well. Do not impose an image percentage, spend the image budget for its own sake, or infer visual fitness from role count alone. Record a concise rationale for the chosen mode **and** why the other mode would be less effective. The suitability judgment remains Luna's within the existing selection call; code should enforce objective constraints, not pretend to score aesthetic quality deterministically.

### Implementation sequence (steps 1–3 done; live proof in step 4 pending)

1. **Eligibility and shared contract.** In the compiled beat menu, make an image an ordinary candidate for each non-closing spoken beat, independent of role count. Keep the opening-hook and audience-close rules. Update the same eligibility check in draft validation so the planner is not offered a choice the validator later rejects. Do not relax the two-cue output contract or image/design asset-type consistency.

2. **Selection request.** In the existing `gpt-6-luna` visual-selection request, replace “generated image if no family fits” with the visual-fitness decision above. Supply the beat's spoken idea, duration estimate, feasible treatment contracts and image constraints together. Ask for one concrete focal moment when selecting an image, plus a reason it carries the necessary spoken roles. Ask why an eligible diagram would add—or fail to add—explanatory value. Keep the existing separate speech-partition call; add no call and do not let the selection call alter approved narration or beat boundaries.

3. **Asset route and screen fitness.** Preserve current authentic-image search, provenance/rights checks, safe generated fallback and project budget. Ensure the chosen image brief specifies the subject, observable moment and composition without claiming a generated historical source. Check that the proposed two or fewer overlays cover only information the image cannot convey by itself, arrive with speech, remain readable, and leave no long static stretch. Existing image motion and overlay treatments may be reused; new image effects are outside this decision unless isolated tests show a concrete gap.

4. **Regression proof.** Add fixtures with one- and multi-role image-suitable beats, a genuine quantitative comparison, a multi-stage process, an authentic-media miss, a budget-exhausted case, and the protected closing beat. Verify the menu and validator agree; image scenes never acquire a code-rendered family; designed scenes never request a full-screen image; overlays stay within two cues and in spoken order; and budget/rights controls still hold. Evaluate a small set of Sun beats and unrelated scripts for *reasons* and visual clarity, not just image count. Render isolated mode-changed beats before any full-film run.

### Acceptance criteria

- A beat with two spoken roles can be image-led when a single visual moment genuinely explains both; it is not rejected solely for having two roles.
- A comparison or process that needs explicit explanatory structure remains designed even when an image is available.
- The planner records a credible image-versus-treatment rationale, and selected imagery supports the spoken idea without fabricated documentary evidence or a collage of separate ideas.
- Image overlays, motion, asset provenance, licensing and request budgets retain their current safety checks. If a selected image cannot be sourced or generated within those checks, the project reports the real asset/budget failure rather than silently switching to an unrelated code card.

## Combined rollout and review gates

Implement Workstream A through the story/script boundary first, then Workstream B against the resulting **approved script**. This order matters: changed chapter or narration boundaries change the input to visual planning. The normal treatment, script, visual-plan and prototype approval gates remain separate; neither workstream grants automatic approval. Use offline fixtures before live generation, then one new versioned Sun candidate and unrelated-script fixtures as proof. Preserve the approved Sun v3 project and assets; no mass migration of prior approved projects. The two workstreams can be reviewed and authorized together, but a failed test or visual choice should have its own RCA rather than triggering a wholesale replan.

## Scope and decision point

This implementation changes the **existing** long-form treatment, chapter-writing and visual-selection requests; it adds no model call. It retains the agreed routing: `gpt-6.1-sol` for treatment/script and `gpt-6-luna` for visual selection and audit. TTS model/voice/speed, the 210-second planning target and 600-second ceiling, research, image-generation model, and other v4 design-family changes are not part of this change. The next production proof is a separately versioned Sun treatment and script followed by the ordinary human gates; offline tests do not establish the creative quality of a new live cut.

The API references used for the narrow implementation boundary are OpenAI's [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses) (keep schema changes compatible with its supported subset) and [Speech API reference](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create) (delivery instructions and speed are separate controls; neither needs a global change for this plan).
