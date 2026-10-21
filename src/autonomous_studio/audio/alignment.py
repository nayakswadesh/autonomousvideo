"""Map approved narration phrases onto measured word timestamps."""

from __future__ import annotations

import re
from difflib import SequenceMatcher


def tokens(value: str) -> list[str]:
    return re.findall(r"\w+", value.casefold(), flags=re.UNICODE)


def estimated_words(text: str, duration: float) -> list[dict]:
    """Deterministic fixture timing; never used as live production alignment."""
    words = re.findall(r"\S+", text)
    return [
        {
            "word": word,
            "start": index * duration / len(words),
            "end": (index + 1) * duration / len(words),
        }
        for index, word in enumerate(words)
    ]


def phrase_start(text: str, phrase: str, words: list[dict], *, offset: float = 0.0) -> float:
    """Use actual transcribed word times, tolerating small ASR spelling differences."""
    script = tokens(text)
    target = tokens(phrase)
    if not target or not script or not words:
        raise ValueError("A spoken phrase and word timestamps are required")
    positions = [
        i for i in range(len(script) - len(target) + 1) if script[i : i + len(target)] == target
    ]
    if not positions:
        # Older approved plans could hit the anchor's character limit midway through
        # its last word. Recover only an unambiguous trailing prefix; future plans
        # reject such anchors during visual-plan validation.
        partial = [
            i
            for i in range(len(script) - len(target) + 1)
            if len(target) >= 2
            and len(target[-1]) >= 3
            and script[i : i + len(target) - 1] == target[:-1]
            and script[i + len(target) - 1].startswith(target[-1])
            and script[i + len(target) - 1] != target[-1]
        ]
        if len(partial) != 1:
            raise ValueError(f"Phrase is absent from approved narration: {phrase}")
        positions = partial
        target[-1] = script[partial[0] + len(target) - 1]
    script_index = positions[0]
    observed = []
    for item in words:
        parts = tokens(str(item["word"]))
        for part in parts:
            observed.append((part, float(item["start"])))
    if not observed:
        raise ValueError("Word timestamps contain no recognized speech")
    observed_tokens = [item[0] for item in observed]
    exact = [
        i
        for i in range(len(observed_tokens) - len(target) + 1)
        if observed_tokens[i : i + len(target)] == target
    ]
    if exact:
        chosen = min(exact, key=lambda i: abs(i / len(observed) - script_index / len(script)))
        return offset + observed[chosen][1]
    matcher = SequenceMatcher(None, script, observed_tokens, autojunk=False)
    if matcher.ratio() < 0.72:
        raise ValueError("Transcription differs too much from approved narration")
    mapped = {
        left + j: right + j
        for left, right, count in matcher.get_matching_blocks()
        for j in range(count)
    }
    if script_index not in mapped:
        nearby = min(mapped, key=lambda index: abs(index - script_index), default=None)
        if nearby is None or abs(nearby - script_index) > 2:
            raise ValueError(f"Could not align spoken phrase: {phrase}")
        script_index = nearby
    return offset + observed[mapped[script_index]][1]
