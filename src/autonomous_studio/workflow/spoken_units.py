"""Stable, topic-neutral speech spans for long-form visual planning."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from autonomous_studio.domain.models import LongFormChapter


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def chapter_units(chapter: LongFormChapter | dict) -> list[dict]:
    """Expose sentence/clause spans; their IDs, not a shot quota, guide the director."""
    result = []
    blocks = (
        chapter.narration_blocks
        if isinstance(chapter, LongFormChapter)
        else chapter["narration_blocks"]
    )
    for block in blocks:
        block_id = block.id if hasattr(block, "id") else block["id"]
        block_text = block.text if hasattr(block, "text") else block["text"]
        cursor = 0
        unit_number = 0
        # Punctuation followed by space supplies optional idea boundaries. A decimal
        # point or the comma in 1,000 remains within its spoken unit.
        unit_version = (
            chapter.get("spoken_unit_version", 1)
            if isinstance(chapter, dict)
            else chapter.spoken_unit_version
        )
        separators = (
            r"(?<=[.!?;:,])\s+|\s*—\s*|\s+–\s+"
            if unit_version >= 2
            else r"(?<=[.!?;:,])\s+"
        )
        pieces = re.split(separators, block_text)
        for piece in pieces:
            count = len(words(piece))
            if not count:
                continue
            unit_number += 1
            result.append(
                {
                    "id": f"{block_id}_u{unit_number}",
                    "block_id": block_id,
                    "start_word": cursor,
                    "end_word": cursor + count,
                    "text": piece.strip(),
                }
            )
            cursor += count
        if cursor != len(words(block_text)):
            raise ValueError(f"Could not segment narration block {block_id}")
    return result


def validate_unit_coverage(chapter: LongFormChapter, beats: list) -> None:
    """Every spoken unit has one visual owner, in speech order and without gaps."""
    units = chapter_units(chapter)
    by_id = {unit["id"]: unit for unit in units}
    expected = [unit["id"] for unit in units]
    actual = [unit_id for beat in beats for unit_id in beat.spoken_unit_ids]
    if actual != expected:
        raise ValueError(
            f"{chapter.id} visual beats must cover spoken units exactly once in speech order"
        )
    for beat in beats:
        if not beat.spoken_unit_ids:
            raise ValueError(f"{beat.id} has no spoken idea")
        block_ids = list(
            dict.fromkeys(by_id[unit_id]["block_id"] for unit_id in beat.spoken_unit_ids)
        )
        if beat.narration_block_ids != block_ids:
            raise ValueError(f"{beat.id} narration blocks disagree with its spoken units")


def beat_words(chapter: LongFormChapter, beat) -> list[str]:
    units = {unit["id"]: unit for unit in chapter_units(chapter)}
    blocks = {block.id: block for block in chapter.narration_blocks}
    return [
        word
        for unit_id in beat.spoken_unit_ids
        for unit in [units[unit_id]]
        for word in words(blocks[unit["block_id"]].text)[unit["start_word"] : unit["end_word"]]
    ]


def unique_phrase_position(spoken: list[str], phrase: str) -> int:
    needle = words(phrase)
    matches = [
        i
        for i in range(len(spoken) - len(needle) + 1)
        if needle and spoken[i : i + len(needle)] == needle
    ]
    if len(matches) != 1:
        raise ValueError(f"spoken anchor must occur exactly once in its assigned idea: {phrase!r}")
    return matches[0]


def _aligned_word_slice(text: str, observed_words: list[dict], start: int, end: int) -> list[dict]:
    """Map script-token boundaries onto ASR words, including split/hyphenated words."""
    script = words(text)
    observed_tokens = []
    word_for_token = []
    for index, item in enumerate(observed_words):
        for token in words(str(item["word"])):
            observed_tokens.append(token)
            word_for_token.append(index)
    if not observed_tokens:
        return []
    matcher = SequenceMatcher(None, script, observed_tokens, autojunk=False)
    if matcher.ratio() < 0.72:
        raise ValueError("Transcription differs too much from approved narration")
    mapping = {
        left + offset: right + offset
        for left, right, count in matcher.get_matching_blocks()
        for offset in range(count)
    }
    positions = [mapping[index] for index in range(start, end) if index in mapping]
    if not positions:
        before = max((index for index in mapping if index < start), default=None)
        after = min((index for index in mapping if index >= end), default=None)
        if before is None or after is None:
            raise ValueError("Spoken unit could not be aligned to measured words")
        preceding = observed_words[word_for_token[mapping[before]]]
        following = observed_words[word_for_token[mapping[after]]]
        # Whisper can absorb omitted words into the preceding word's timestamp.
        # In that case use that word's start, not its stretched end, as the
        # lower bound. This is an estimated cue, not an ASR-confirmed word.
        lower = float(preceding["end"])
        upper = float(following["start"])
        if lower >= upper:
            lower = float(preceding["start"])
        if lower >= upper:
            raise ValueError("Spoken unit could not be aligned to measured words")
        midpoint = (lower + upper) / 2
        return [{
            "word": " ".join(script[start:end]),
            "start": midpoint,
            "end": min(midpoint + 0.1, upper),
            "estimated": True,
        }]
    return observed_words[word_for_token[min(positions)] : word_for_token[max(positions)] + 1]


def spoken_text(chapter: dict, beat: dict) -> str:
    """Preserve approved wording and punctuation in a speech-first scene."""
    units = {unit["id"]: unit for unit in chapter_units(chapter)}
    return " ".join(units[unit_id]["text"] for unit_id in beat["spoken_unit_ids"])


def combined_speech(chapter: dict, beat: dict, windows: dict[str, dict]) -> tuple[dict, dict]:
    """Present one continuous chapter-clock word view across TTS file boundaries."""
    block_ids = beat["narration_block_ids"]
    blocks = {block["id"]: block for block in chapter["narration_blocks"]}
    units = {unit["id"]: unit for unit in chapter_units(chapter)}
    selected = [units[unit_id] for unit_id in beat.get("spoken_unit_ids", [])]
    if not selected:
        selected = [
            {
                "block_id": block_id,
                "start_word": 0,
                "end_word": len(words(blocks[block_id]["text"])),
            }
            for block_id in block_ids
        ]
    speech = (
        spoken_text(chapter, beat)
        if beat.get("spoken_unit_ids")
        else " ".join(blocks[block_id]["text"] for block_id in block_ids)
    )
    absolute_words = [
        {**word, "start": windows[unit["block_id"]]["start"] + float(word["start"])}
        for unit in selected
        for word in _aligned_word_slice(
            blocks[unit["block_id"]]["text"],
            windows[unit["block_id"]].get("words", []),
            unit["start_word"],
            unit["end_word"],
        )
    ]
    start = absolute_words[0]["start"] if absolute_words else windows[block_ids[0]]["start"]
    merged_words = [{**word, "start": word["start"] - start} for word in absolute_words]
    last_window = windows[block_ids[-1]]
    last_words = _aligned_word_slice(
        blocks[block_ids[-1]]["text"],
        last_window.get("words", []),
        selected[-1]["start_word"],
        selected[-1]["end_word"],
    )
    end = last_window["start"] + float(last_words[-1]["end"]) if last_words else last_window["end"]
    return (
        {"id": "+".join(block_ids), "text": speech},
        {"block_id": "+".join(block_ids), "start": start, "end": end, "words": merged_words},
    )
