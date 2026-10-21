"""Deterministic normalization for structured ranking research."""

from __future__ import annotations

from copy import deepcopy

from autonomous_studio.domain.models import infer_numeric_ranking_direction


class AmbiguousRankingError(ValueError):
    """The supplied numeric claims cannot establish either ranking direction."""


def normalize_ranking_direction(data: dict) -> tuple[dict, dict | None]:
    """Correct only a clear direction mismatch; never change claims, values, or ranks."""
    normalized = deepcopy(data)
    ranking = normalized.get("ranking")
    subjects = normalized.get("subjects") or []
    claims = {claim.get("id"): claim for claim in normalized.get("claims") or []}
    ranked = [subject for subject in subjects if subject.get("rank") is not None]
    if not ranking or not ranked:
        return normalized, None

    try:
        ordered = sorted(ranked, key=lambda subject: int(subject["rank"]))
        selected = [claims[subject["claim_id"]] for subject in ordered]
        if not selected or not all(claim.get("kind") == "numeric" for claim in selected):
            return normalized, None
        values = [float(claim["value"]) for claim in selected]
    except (KeyError, TypeError, ValueError):
        return normalized, None

    inferred = infer_numeric_ranking_direction(values)
    if inferred is None:
        raise AmbiguousRankingError(
            "Ranking direction is ambiguous: numeric claim values are neither uniquely "
            "ascending nor descending by rank. Revise the metric or provide a usable "
            "tie-break value."
        )
    supplied = ranking.get("direction")
    if supplied == inferred:
        return normalized, None
    ranking["direction"] = inferred
    return normalized, {
        "kind": "ranking_direction",
        "from": supplied,
        "to": inferred,
        "reason": "rank-ordered numeric claim values establish the opposite direction",
        "values": values,
    }
