"""Resolve reviewed sources from approved spoken claim links on demand."""

from collections.abc import Iterable


def source_ids_for_spoken_blocks(
    research: dict, chapter: dict, narration_block_ids: Iterable[str]
) -> list[str]:
    """Return sources backing claims attached to the selected spoken blocks.

    This is provenance lookup, not a visual-planning input. It deliberately does
    not infer sources for facts introduced only by a diagram or an image.
    """
    block_ids = set(narration_block_ids)
    blocks = {block["id"]: block for block in chapter["narration_blocks"]}
    missing_blocks = block_ids - blocks.keys()
    if missing_blocks:
        raise ValueError(f"unknown narration blocks: {sorted(missing_blocks)}")
    claim_ids = {
        claim_id
        for block_id in block_ids
        for claim_id in blocks[block_id].get("claim_ids", [])
    }
    claims = {claim["id"]: claim for claim in research["claims"]}
    missing_claims = claim_ids - claims.keys()
    if missing_claims:
        raise ValueError(f"unknown spoken claims: {sorted(missing_claims)}")
    return sorted({
        source_id
        for claim_id in claim_ids
        for source_id in claims[claim_id]["source_ids"]
    })
