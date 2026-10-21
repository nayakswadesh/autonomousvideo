"""Keep old long-form decisions for audit without replaying obsolete instructions."""

from __future__ import annotations

import re

_IMAGE_WORD = re.compile(r"\b(?:images?|illustrations?|visuals?|image[- ]requests?)\b", re.I)
_NUMBER = re.compile(
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
    r"nineteen|twenty|thirty)\b",
    re.I,
)
_QUOTA = re.compile(
    r"\b(?:budget|cap|limit|quota|reserve|allocate|allow|up to|at most|"
    r"no more than|maximum|exactly|only|requests? used)\b",
    re.I,
)


def has_image_quota(text: str) -> bool:
    """A free-text image count is not a policy; the structured setting is authoritative."""
    return bool(_IMAGE_WORD.search(text) and _NUMBER.search(text) and _QUOTA.search(text))


def remove_image_quotas(text: str, *, clauses: bool = False) -> str:
    separator = r"(?<=[.!?;,])\s+" if clauses else r"(?<=[.!?])\s+"
    sentences = re.split(separator, text.strip())
    return " ".join(sentence for sentence in sentences if not has_image_quota(sentence)).strip()


def relevant_longform_feedback(project) -> list[str]:
    """Carry creative feedback since the latest fresh restart, never numeric image quotas."""
    revisions = [
        item for item in project.feedback if item.decision == "revise" and item.message.strip()
    ]
    restart_indices = [
        index
        for index, item in enumerate(revisions)
        if item.message.lstrip().lower().startswith(("treatment:", "restart:", "research:"))
    ]
    if restart_indices:
        revisions = revisions[restart_indices[-1] :]
    return [
        clean for item in revisions if (clean := remove_image_quotas(item.message, clauses=True))
    ]
