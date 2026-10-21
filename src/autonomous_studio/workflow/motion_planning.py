"""Deterministic, opt-in conversion of approved long-form beats into motion plans.

The existing visual director supplies story purposes and asset choices. This layer
chooses only supported motion templates, re-times them to measured narration, and
declines to display generated archival-looking pictures as genuine evidence.
"""

from __future__ import annotations

import re
from collections import defaultdict

from autonomous_studio.audio.alignment import phrase_start, tokens
from autonomous_studio.domain.motion import MotionPlan, validate_motion_plan

MOTION_PLANNER_VERSION = "narration-linked-visual-beat-adapter-v6-family-first"

_HISTORICAL_SIMULATION = re.compile(
    r"\b(portrait|archiv(?:al|e)[ -](?:looking|style|photo\w*|portrait|scan|"
    r"facsimile|document|page|manuscript|handwriting)|"
    r"historical (?:photo\w*|notebook|document)|authenticated|manuscript|"
    r"laboratory notebook)\b",
    re.IGNORECASE,
)


def _historical_simulation(prompt: str) -> bool:
    """Negative prompt instructions must not classify a concept image as fake archive."""
    positive_request = re.sub(
        r"\b(?:avoid|do not|without|no|not|rather than|instead of|as opposed to)\b"
        r"[^.;:]*?(?=\bbut\b|,\s*(?:show|create|depict|render|include)\b|[.;:]|$)",
        "",
        prompt,
        flags=re.I,
    )
    # A contemporary illustration of a closed artefact is not a reproduction
    # of its contents. Keep it image-led rather than creating an unapproved
    # code-rendered replacement in the family-first production path.
    if (
        re.search(r"\bcontemporary editorial illustration\b", positive_request, re.I)
        and re.search(r"\bclosed\b.{0,80}\b(?:notebook|document|book)\b", positive_request, re.I)
        and not re.search(
            r"\b(?:portrait|photo\w*|facsimile|manuscript|page|handwriting|signature|authenticated)\b",
            positive_request,
            re.I,
        )
    ):
        return False
    return bool(_HISTORICAL_SIMULATION.search(positive_request))


def _present_image_shot(shot: dict, asset: dict, previous: dict | None) -> None:
    """Select reusable image presentation without changing approved words or cue times."""
    if shot.get("kind") != "camera":
        return
    duration = shot["end"] - shot["start"]
    direction = asset.get("direction") or asset.get("fallback_direction") or {}
    # The image is already normalized to the output aspect ratio. A legacy
    # bottom crop could erase a generated focal subject; never carry it into a
    # newly planned image presentation merely to conceal unwanted AI text.
    shot["crop_bottom"] = 0.0
    safe = direction.get("overlay_safe_region", "none")
    focal = direction.get("focal_placement", "").casefold()
    edge_subject = bool(
        re.search(r"\b(?:low|lower|bottom|upper|top)\b", focal)
        and re.search(r"\b(?:left|right|edge|corner)\b", focal)
    ) or bool(re.search(r"\b(?:edge|corner)\b", focal))
    prior_effect = previous.get("label_effect") if previous else None
    prior_style = previous.get("motion_style") if previous else None
    label = shot.get("label", "")
    if len(label) > 48:
        effect = "slide_plate"
    elif len(label) <= 44 and ("?" in label or re.search(r"\d", label)):
        effect = "gentle_pop"
    elif len(label) <= 32:
        effect = "wipe"
    else:
        effect = "fade_rise"
    if effect == prior_effect and effect in {"wipe", "fade_rise", "gentle_pop"}:
        effect = "fade_rise" if effect != "fade_rise" else "wipe"
    shot["label_effect"] = effect
    # Within the region reserved by the approved image direction, alternate
    # upper/middle placement. Unknown photographic composition stays protected.
    if safe == "left":
        shot["text_zone"] = "middle_left" if previous and previous.get("text_zone") == "upper_left" else "upper_left"
    elif safe == "right":
        shot["text_zone"] = "middle_right" if previous and previous.get("text_zone") == "upper_right" else "upper_right"
    elif safe == "upper":
        shot["text_zone"] = "upper_right" if previous and previous.get("text_zone") == "upper_left" else "upper_left"
    else:
        shot["text_zone"] = "upper_left"
        shot["label_effect"] = "slide_plate"

    if shot.get("presentation") == "contain":
        # No source crop is allowed merely to add movement to archival material.
        shot.update(motion_style="hold", zoom_start=1.0, zoom_end=1.0,
                    focus_start=(0.5, 0.5), focus_end=(0.5, 0.5))
    elif duration < 5.0 or edge_subject:
        shot.update(motion_style="hold", zoom_start=1.0, zoom_end=1.0,
                    focus_start=(0.5, 0.5), focus_end=(0.5, 0.5))
    elif duration >= 8.0 and prior_style not in {"hold_then_push", "push_in"}:
        shot.update(motion_style="hold_then_push", hold_fraction=0.27,
                    zoom_start=1.02, zoom_end=1.09,
                    focus_start=(0.5, 0.5), focus_end=(0.5, 0.5))
    elif prior_style != "pull_back":
        shot.update(motion_style="pull_back", zoom_start=1.09, zoom_end=1.02,
                    focus_start=(0.5, 0.5), focus_end=(0.5, 0.5))
    else:
        shot.update(motion_style="push_in", zoom_start=1.02, zoom_end=1.09,
                    focus_start=(0.5, 0.5), focus_end=(0.5, 0.5))


_STOPWORDS = {
    "a",
    "about",
    "and",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "this",
    "to",
    "was",
    "were",
    "what",
    "which",
    "with",
    "would",
    "work",
    "image",
    "visualize",
    "show",
    "present",
    "explain",
}


def _tokens(value: str) -> list[str]:
    return [token.casefold() for token in re.findall(r"\w+", value)]


def _semantic_anchor(beat: dict, block: dict, window: dict) -> tuple[float | None, int]:
    """Estimate the first relevant spoken phrase without needing burned captions."""
    explicit = beat.get("narration_anchor", "").strip()
    if explicit:
        if window.get("words"):
            return (
                phrase_start(block["text"], explicit, window["words"], offset=window["start"]),
                100,
            )
        spoken_tokens = tokens(block["text"])
        anchor_tokens = tokens(explicit)
        index = next(
            (
                i
                for i in range(len(spoken_tokens) - len(anchor_tokens) + 1)
                if spoken_tokens[i : i + len(anchor_tokens)] == anchor_tokens
            ),
            None,
        )
        if index is None:
            raise ValueError(f"Scene anchor is absent from narration: {explicit}")
        return window["start"] + index / max(1, len(spoken_tokens)) * (
            window["end"] - window["start"]
        ), 100
    spoken = _tokens(block["text"])
    if not spoken:
        return None, 0
    keywords = {}
    for token in _tokens(beat["purpose"]):
        if len(token) > 3 and token not in _STOPWORDS:
            keywords[token] = max(keywords.get(token, 0), 1)
    labels = beat.get("overlay_text", [])
    for label_index, label in enumerate(labels):
        weight = 3
        if beat["visual_type"] == "comparison" and len(labels) == 2:
            # The second side usually carries the distinction/reveal.
            weight = 4 if label_index == 1 else 1
        for token in _tokens(label):
            if len(token) > 3 and token not in _STOPWORDS:
                keywords[token] = max(keywords.get(token, 0), weight)
    if not keywords:
        return None, 0
    candidates = []
    for index in range(len(spoken)):
        window_tokens = set(spoken[index : index + 8])
        score = sum(weight for token, weight in keywords.items() if token in window_tokens)
        if score:
            at = window["start"] + index / len(spoken) * (window["end"] - window["start"])
            candidates.append((score, -abs(at - beat["start"]), -index, at))
    if not candidates:
        return None, 0
    score, _, _, at = max(candidates)
    return at, score


def _scene_entry_anchor(beat: dict, block: dict, window: dict) -> tuple[float | None, int]:
    """Enter by the first spoken reveal without moving a later image-camera cue."""
    anchor, score = _semantic_anchor(beat, block, window)
    reveals = beat.get("overlay_anchors") or []
    if not beat.get("spoken_unit_ids") or not reveals:
        return anchor, score
    first_reveal, _ = _semantic_anchor(
        {**beat, "narration_anchor": reveals[0]}, block, window
    )
    if first_reveal is None:
        raise ValueError(f"{beat['id']} has no timed first reveal")
    return (min(anchor, first_reveal) if anchor is not None else first_reveal), score


def _excerpt(text: str, fraction: float) -> str:
    words = text.split()
    if not words:
        raise ValueError("Narration block is empty")
    start = min(max(0, round(fraction * len(words))), max(0, len(words) - 5))
    return " ".join(words[start : start + 5])


def _cue(
    shot: dict,
    block: dict,
    window: dict,
    action: str,
    fraction: float,
    *,
    index=None,
    phrase: str = "",
):
    start, end = shot["start"], shot["end"]
    if phrase and window.get("words"):
        measured = phrase_start(block["text"], phrase, window["words"], offset=window["start"])
        if measured < start - 0.7 or measured > end + 0.1:
            raise ValueError(
                f"Spoken phrase for {shot['id']} falls outside its scene; replan scene timing"
            )
        at = min(end - 0.28, max(start + 0.04, measured))
    else:
        at = start + max(0.12, (end - start) * fraction)
        at = min(at, end - 0.48, window["end"] + 0.4)
        at = max(start + 0.04, at)
    spoken_fraction = min(
        0.95, max(0.0, (at - window["start"]) / max(0.1, window["end"] - window["start"]))
    )
    cue = {
        "at_seconds": round(at, 5),
        "narration_excerpt": phrase or _excerpt(block["text"], spoken_fraction),
        "action": action,
        "duration_seconds": 0.36,
    }
    if index is not None:
        cue["step_index"] = index
    return cue


def _closing_clauses(narration: str) -> tuple[str, str]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", narration) if part.strip()]
    ending = sentences[-1].rstrip(".?!")
    if ";" in ending:
        first, second = ending.split(";", 1)
    else:
        words = ending.split()
        middle = max(1, len(words) // 2)
        first, second = " ".join(words[:middle]), " ".join(words[middle:])
    second = second.strip()
    return first.strip()[:100], (second[:1].upper() + second[1:])[:100]


def _shot_for_beat(beat: dict, asset: dict, block: dict, window: dict, shot: dict):
    labels = [str(label).strip() for label in beat.get("overlay_text", []) if str(label).strip()]
    anchors = beat.get("overlay_anchors", [])
    sequence_pairs = []
    deferred_labels = []
    if len(labels) == len(anchors) and window.get("words"):
        for label, anchor in zip(labels, anchors, strict=True):
            spoken_at = phrase_start(block["text"], anchor, window["words"], offset=window["start"])
            if shot["start"] - 0.7 <= spoken_at <= shot["end"] - 0.25:
                sequence_pairs.append((label, anchor))
            else:
                deferred_labels.append(label)
        if sequence_pairs:
            labels = [label for label, _ in sequence_pairs]
            anchors = [anchor for _, anchor in sequence_pairs]
    asset_kind = asset.get("kind", "")
    asset_prompt = asset.get("prompt", "")
    reason = ""
    special_phrases = {}
    if beat["visual_type"] == "outro":
        questions = re.findall(r"[^.!?]*\?", block["text"])
        subscribe = next(
            (
                sentence
                for sentence in re.split(r"(?<=[.!?])\s+", block["text"])
                if "subscribe" in sentence.casefold()
            ),
            "",
        )
        if not questions or not subscribe or not labels:
            raise ValueError("The closing scene needs spoken recap labels, a question and a CTA")
        question = questions[-1].strip()
        shot.update(
            kind="branded_outro",
            intent="close",
            recap_items=labels[:4],
            question=question[:180],
            cta="LIKE  ·  SHARE  ·  SUBSCRIBE",
            why_this_template=(
                "The spoken recap, viewer question and channel invitation share one close."
            ),
        )
        actions = [("reveal_step", 0.08 + 0.12 * i, i) for i in range(len(labels[:4]))]
        actions += [("show_question", 0.68), ("show_label", 0.85)]
        special_phrases = {"show_question": question, "show_label": subscribe}
        reason = "narration_linked_branded_close"
    elif (
        beat["visual_type"] in {"map", "timeline", "document", "kinetic_title", "chapter_card"}
        and len(sequence_pairs) >= 2
    ):
        layout = (
            "timeline"
            if beat["visual_type"] in {"map", "timeline"} and len(sequence_pairs) <= 4
            else "stack"
            if beat["visual_type"] == "document"
            else "carousel"
            if len(sequence_pairs) > 6
            else "flow"
        )
        layout = beat.get("display_layout") or layout
        shot.update(
            kind="narration_sequence",
            intent=(
                "locate"
                if beat["visual_type"] == "map"
                else "examine_evidence"
                if layout == "stack"
                else "explain_process"
            ),
            layout=layout,
            entries=[label[:100] for label, _ in sequence_pairs[:8]],
            heading=beat.get("screen_title", "")[:100],
            why_this_template=(
                "Every planned point enters on its spoken anchor; the scene remains "
                "one evolving visual rather than dropping later points."
            ),
        )
        actions = [("reveal_step", 0.08 + 0.17 * i, i) for i in range(len(shot["entries"]))]
        reason = "spoken_order_sequence"
    elif (
        beat["visual_type"] == "data_chart"
        and len(sequence_pairs) >= 2
        and not re.search(r"\b(mismatch|discrepancy|expected|versus|vs\.)\b", beat["purpose"], re.I)
    ):
        shot.update(
            kind="narration_sequence",
            intent="explain_process",
            layout=beat.get("display_layout") or "flow",
            entries=[label[:100] for label, _ in sequence_pairs[:8]],
            heading=beat.get("screen_title", "")[:100],
            why_this_template=(
                "The narration describes a sequence, not a quantitative mismatch; "
                "show each stage on its spoken entrance."
            ),
        )
        actions = [("reveal_step", 0.08 + 0.15 * i, i) for i in range(len(shot["entries"]))]
        reason = "qualitative_data_as_sequence"
    elif (
        asset_kind == "generated_image"
        and _historical_simulation(asset_prompt)
        and len(sequence_pairs) >= 3
    ):
        shot.update(
            kind="narration_sequence",
            intent="connect",
            layout=beat.get("display_layout") or "flow",
            entries=[label[:100] for label, _ in sequence_pairs[:8]],
            heading=beat.get("screen_title", "")[:100],
            why_this_template=(
                "A narration-led relationship avoids presenting synthetic historical "
                "imagery as authentic evidence."
            ),
        )
        actions = [("reveal_step", 0.08 + 0.15 * i, i) for i in range(len(shot["entries"]))]
        reason = "historical_simulation_replaced_with_sequence"
    elif asset_kind == "generated_image" and _historical_simulation(asset_prompt):
        # An AI portrait or notebook cannot become an authentic historical exhibit.
        shot.update(
            kind="fact_reveal",
            intent="examine_evidence",
            eyebrow=beat.get("screen_title", "")[:100],
            line_1=(labels[0] if labels else _excerpt(block["text"], 0.2))[:100],
            line_2=labels[1][:100] if len(labels) > 1 else "",
            why_this_template=(
                "A factual card avoids presenting a synthetic historical-looking "
                "image as an authentic record."
            ),
        )
        actions = [("show_line_1", 0.09)]
        if len(labels) > 1:
            actions.append(("show_line_2", 0.48))
        reason = "suppressed_generated_historical_simulation"
    elif beat["visual_type"] == "data_chart" and len(labels) >= 2:
        # No calibrated numerical values: a categorical gap is honest; bars are not.
        shot.update(
            kind="evidence_gap",
            intent="compare",
            title="A MISMATCH IN THE EVIDENCE",
            expected_label=labels[0][:100],
            observed_label=labels[1][:100],
            gap_label="MISMATCH",
            why_this_template=(
                "The narration describes a discrepancy but gives no numeric scale, "
                "so this graphic conveys difference without invented bar lengths."
            ),
        )
        actions = [("show_expected", 0.08), ("show_observed", 0.39), ("show_gap", 0.72)]
        reason = "qualitative_data_no_invented_numbers"
    elif beat["visual_type"] == "comparison" and len(labels) >= 2:
        # Text labels alone do not prove two numbers share a comparable unit or
        # denominator. Numeric bars require an explicitly typed upstream datum.
        if len(sequence_pairs) >= 3 and beat.get("display_layout"):
            shot.update(
                kind="narration_sequence",
                intent="connect",
                layout=beat["display_layout"],
                entries=[label[:100] for label, _ in sequence_pairs[:8]],
                heading=beat.get("screen_title", "")[:100],
                why_this_template=(
                    "The director supplied a semantic layout for multiple narrated ideas; "
                    "each arrives with its measured spoken phrase."
                ),
            )
            actions = [("reveal_step", 0.08 + 0.12 * i, i) for i in range(len(shot["entries"]))]
            reason = "directed_semantic_comparison"
        elif len(labels) >= 3:
            shot.update(
                kind="relationship_map",
                intent="connect",
                title=beat.get("screen_title") or "CONNECTED CONTRIBUTIONS",
                nodes=[label[:90] for label in labels[:4]],
                why_this_template=(
                    "The beat compares several roles, which are relationships "
                    "rather than a numeric ranking or sequence."
                ),
            )
            actions = [("reveal_step", 0.08 + i * 0.26, i) for i in range(len(shot["nodes"]))]
            reason = "qualitative_relationships"
        else:
            shot.update(
                kind="concept_contrast",
                intent="compare",
                title=f"{labels[0].split(':')[0]} vs. {labels[1].split(':')[0]}"[:90],
                left_label=labels[0][:100],
                right_label=labels[1][:100],
                verdict_style=(
                    "correction"
                    if re.search(
                        r"\b(?:rather than|not merely|not simply|instead of)\b", block["text"], re.I
                    )
                    and not re.search(
                        r"\b(?:might|perhaps|possibly|uncertain)\b", block["text"], re.I
                    )
                    else "neutral"
                ),
                why_this_template=(
                    "Two distinct ideas are contrasted without implying a made-up numerical scale."
                ),
            )
            actions = [("reveal_left", 0.08), ("reveal_right", 0.54)]
            reason = "qualitative_contrast"
    elif asset_kind in {"authentic_image", "generated_image"} and beat.get("asset_ref"):
        shot.update(
            kind="camera",
            intent="establish",
            asset_ref=beat["asset_ref"],
            presentation="contain" if asset_kind == "authentic_image" else "fill",
            crop_bottom=0.26,
            zoom_start=1.0 if asset_kind == "authentic_image" else 1.04,
            zoom_end=1.04 if asset_kind == "authentic_image" else 1.15,
            focus_start=(0.46, 0.43),
            focus_end=(0.54, 0.41),
            label=labels[0][:80] if labels else "",
            secondary_label=labels[1][:100] if len(labels) > 1 else "",
            why_this_template=(
                "A restrained camera move supports the physical scene without "
                "asserting unsupported data or depth layers."
            ),
        )
        actions = [("start_move", 0.06)]
        if labels:
            actions.append(("show_label", 0.08))
        if len(labels) > 1:
            actions.append(("show_secondary_label", 0.52))
        reason = "single_flat_image_camera_move"
    else:
        shot.update(
            kind="fact_reveal",
            intent="establish",
            eyebrow=beat.get("screen_title", "")[:100],
            line_1=(labels[0] if labels else _excerpt(block["text"], 0.2))[:100],
            line_2=labels[1][:100] if len(labels) > 1 else "",
            why_this_template=(
                "A text reveal is the safe fallback when no supported image "
                "or data treatment is available."
            ),
        )
        actions = [("show_line_1", 0.08)]
        if len(labels) > 1:
            actions.append(("show_line_2", 0.5))
        reason = "unsupported_asset_fallback"
    shot["cues"] = []
    anchors = (
        [anchor for _, anchor in sequence_pairs]
        if shot["kind"] == "narration_sequence"
        else anchors
    )
    display_index = 0
    for action, fraction, *rest in actions:
        if action in special_phrases:
            anchor = special_phrases[action]
        elif action == "start_move":
            anchor = beat.get("narration_anchor", "")
        elif action == "show_gap":
            anchor = anchors[-1] if anchors else beat.get("narration_anchor", "")
        else:
            anchor = anchors[display_index] if display_index < len(anchors) else ""
            display_index += 1
        shot["cues"].append(
            _cue(
                shot,
                block,
                window,
                action,
                fraction,
                index=rest[0] if rest else None,
                phrase=anchor,
            )
        )
    return shot, reason, deferred_labels


def plan_motion_chapter(
    project: dict,
    chapter_id: str,
    speech_windows: list[dict],
    *,
    comparison_name: str | None = None,
    width: int = 1280,
    height: int = 720,
    fps: int = 24,
    family_first: bool = False,
) -> tuple[MotionPlan, dict]:
    """Build measured boundaries; v2 designed beats use neutral, never-rendered scaffolds."""
    chapter = next((item for item in project["spec"]["chapters"] if item["id"] == chapter_id), None)
    if chapter is None:
        raise ValueError(f"Unknown chapter: {chapter_id}")
    if not speech_windows:
        raise ValueError("Measured narration windows are required")
    windows = {item["block_id"]: item for item in speech_windows}
    blocks = chapter["narration_blocks"]
    assets = {item["id"]: item for item in project["longform_visual_plan"]["assets"]}
    speech_first = any(beat.get("spoken_unit_ids") for beat in chapter["visual_beats"])
    groups = defaultdict(list)
    if speech_first:
        from autonomous_studio.workflow.spoken_units import combined_speech

        if not all(beat.get("spoken_unit_ids") for beat in chapter["visual_beats"]):
            raise ValueError("Speech-first chapter cannot mix planned and legacy beats")
        groups["chapter"] = chapter["visual_beats"]
        segments = [{"id": "chapter", "text": " ".join(b["text"] for b in blocks)}]
    else:
        for beat in chapter["visual_beats"]:
            if len(beat["narration_block_ids"]) != 1:
                raise ValueError(f"Beat {beat['id']} must refer to exactly one narration block")
            groups[beat["narration_block_ids"][0]].append(beat)
        if set(groups) != {block["id"] for block in blocks} or not set(groups) <= windows.keys():
            raise ValueError("Every narration block needs beats and a measured speech window")
        segments = blocks
    shots, decisions = [], []
    segment_start = 0.0
    duration = chapter["target_duration"]
    for block_index, block in enumerate(segments):
        window = (
            {"start": windows[blocks[0]["id"]]["start"], "end": windows[blocks[-1]["id"]]["end"]}
            if speech_first
            else windows[block["id"]]
        )
        if block_index + 1 < len(segments):
            next_window = windows[segments[block_index + 1]["id"]]
            # Hold the prior scene through a real narration pause. Splitting the
            # gap at its midpoint would reveal the next designed scene before
            # its words begin, placing its first cue outside measured speech.
            segment_end = (
                next_window["start"]
                if next_window["start"] >= window["end"]
                else (window["end"] + next_window["start"]) / 2
            )
        else:
            segment_end = duration
        beat_anchors = []
        for beat in groups[block["id"]]:
            beat_block, beat_window = (
                combined_speech(chapter, beat, windows) if speech_first else (block, window)
            )
            anchor, score = _scene_entry_anchor(beat, beat_block, beat_window)
            beat_anchors.append((beat, anchor, score, beat_block, beat_window))
        if not speech_first:
            beat_anchors.sort(
                key=lambda item: (
                    item[1] if item[1] is not None else item[0]["start"],
                    item[0]["start"],
                )
            )
        cursor = segment_start
        for beat_index, (beat, anchor, score, beat_block, beat_window) in enumerate(beat_anchors):
            if speech_first and beat_index > 0 and anchor is not None and cursor > anchor + 0.1:
                raise ValueError(
                    f"{beat['id']} first spoken reveal would precede its scene; "
                    "replan beat boundaries"
                )
            asset_id = beat.get("asset_ref")
            if asset_id not in assets:
                raise ValueError(f"Missing planned asset for beat {beat['id']}: {asset_id}")
            if asset_id not in project["longform_media"]:
                raise ValueError(f"Missing acquired asset for beat {beat['id']}: {asset_id}")
            if beat_index == len(beat_anchors) - 1:
                end = segment_end
            else:
                next_beat, next_anchor, _, _, _ = beat_anchors[beat_index + 1]
                desired = next_anchor if next_anchor is not None else next_beat["start"]
                remaining = len(beat_anchors) - beat_index - 1
                end = min(max(cursor + 1.2, desired), segment_end - 1.2 * remaining)
            if end <= cursor:
                raise ValueError(f"{beat['id']} has no readable screen time")
            shot = {
                "id": beat["id"],
                "start": round(cursor, 6),
                "end": round(end, 6),
                "viewer_takeaway": beat["purpose"],
                "narration_block_ids": beat["narration_block_ids"],
                "claim_ids": beat["claim_ids"] or chapter["evidence_claim_ids"],
            }
            if family_first and beat["visual_type"] not in {"authentic_image", "generated_image"}:
                label = next(
                    (item.strip() for item in beat.get("overlay_text", []) if item.strip()),
                    "",
                )
                shot.update(
                    kind="fact_reveal",
                    intent="establish",
                    eyebrow=beat.get("screen_title", "")[:100],
                    line_1=(label or beat["purpose"])[:100],
                    line_2="",
                    why_this_template=(
                        "Timing scaffold only; a registered family replaces this before rendering."
                    ),
                    cues=[_cue(shot, beat_block, beat_window, "show_line_1", 0.08)],
                )
                reason, deferred_labels = "family_first_timing_scaffold", []
            else:
                if family_first and beat["visual_type"] in {"authentic_image", "generated_image"}:
                    asset = assets[asset_id]
                    if asset["kind"] != beat["visual_type"]:
                        raise ValueError(
                            f"{beat['id']} approved image type disagrees with its asset; "
                            "replan the visual before rendering"
                        )
                    if asset["kind"] == "generated_image" and _historical_simulation(
                        asset.get("prompt", "")
                    ):
                        raise ValueError(
                            f"{beat['id']} requests a simulated historical image; "
                            "replan the visual rather than substituting a code scene"
                        )
                shot, reason, deferred_labels = _shot_for_beat(
                    beat, assets[asset_id], beat_block, beat_window, shot
                )
                if speech_first and deferred_labels:
                    raise ValueError(
                        f"{beat['id']} spoken labels fall outside their scene; "
                        "replan beat boundaries"
                    )
                if (
                    family_first
                    and beat["visual_type"] in {"authentic_image", "generated_image"}
                    and shot["kind"] not in {"camera", "detail_reveal", "guided_image"}
                ):
                    raise ValueError(
                        f"{beat['id']} approved image became a code scene; "
                        "replan the visual rather than substituting a design family"
                    )
            if (
                beat_index == len(beat_anchors) - 1
                and block_index == len(segments) - 1
                and shot["kind"] == "relationship_map"
                and score < 5
                and not beat.get("narration_anchor")
            ):
                first, second = _closing_clauses(beat_block["text"])
                shot.pop("title")
                shot.pop("nodes")
                shot.update(
                    kind="fact_reveal",
                    intent="foreshadow",
                    viewer_takeaway=second,
                    eyebrow="THE NEXT QUESTION",
                    line_1=first,
                    line_2=second,
                    why_this_template=(
                        "The existing relationship labels are weakly aligned with the "
                        "closing narration, so the final spoken question becomes the visual."
                    ),
                )
                shot["cues"] = [
                    _cue(shot, beat_block, beat_window, "show_line_1", 0.08),
                    _cue(shot, beat_block, beat_window, "show_line_2", 0.49),
                ]
                reason = "closing_narration_replaces_weakly_aligned_relation"
            if shot["kind"] == "camera":
                if (
                    chapter_id == project["spec"]["chapters"][0]["id"]
                    and block_index == 0
                    and beat_index == 0
                ):
                    shot["hook_headline"] = beat.get("hook_headline", "")
                    shot["thumbnail_headline"] = beat.get("thumbnail_headline", "")
                previous_camera = next(
                    (item for item in reversed(shots) if item["kind"] == "camera"), None
                )
                _present_image_shot(shot, assets[asset_id], previous_camera)
            shots.append(shot)
            decisions.append(
                {
                    "beat_id": beat["id"],
                    "asset_ref": asset_id,
                    "selected_template": shot["kind"],
                    "reason": reason,
                    "original_start": beat["start"],
                    "narration_aligned_start": shot["start"],
                    "estimated_spoken_anchor": round(anchor, 3) if anchor is not None else None,
                    "anchor_keyword_score": score,
                    "labels_outside_scene": deferred_labels,
                    "image_motion_style": shot.get("motion_style"),
                    "image_label_effect": shot.get("label_effect"),
                }
            )
            cursor = end
        segment_start = segment_end
    plan = MotionPlan.model_validate(
        {
            "schema_version": 1,
            "project_id": project["id"],
            "chapter_id": chapter_id,
            "comparison_name": comparison_name or f"{chapter_id}-auto-motion",
            "width": width,
            "height": height,
            "fps": fps,
            "shots": shots,
        }
    )
    validate_motion_plan(plan, project, speech_windows)
    quality_flags = []
    for shot in plan.shots:
        if shot.kind in {"camera", "detail_reveal", "document_highlight", "guided_image"}:
            continue
        first_cue = min(cue.at_seconds for cue in shot.cues)
        last_cue = max(cue.at_seconds for cue in shot.cues)
        if first_cue - shot.start > 1.5:
            quality_flags.append(
                {
                    "shot_id": shot.id,
                    "issue": "late_first_reveal",
                    "seconds": round(first_cue - shot.start, 2),
                }
            )
        if shot.end - last_cue > 6.0:
            quality_flags.append(
                {
                    "shot_id": shot.id,
                    "issue": "long_static_tail",
                    "seconds": round(shot.end - last_cue, 2),
                }
            )
    for decision in decisions:
        if decision["labels_outside_scene"]:
            quality_flags.append(
                {
                    "shot_id": decision["beat_id"],
                    "issue": "spoken_labels_outside_scene",
                    "labels": decision["labels_outside_scene"],
                }
            )
    return plan, {
        "planner": MOTION_PLANNER_VERSION,
        "chapter_id": chapter_id,
        "source": "approved narration, measured speech windows, existing visual beats/assets",
        "decisions": decisions,
        "quality_flags": quality_flags,
        "limitations": [
            "Live cues use measured word entrances; mock and legacy plans use estimated timing.",
            "Flat images cannot support true parallax or depth displacement without layers.",
            "Generated historical-looking images are replaced with factual cards, "
            "not treated as archives.",
        ],
    }
