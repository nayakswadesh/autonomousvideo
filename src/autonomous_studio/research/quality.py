from autonomous_studio.domain.models import Brief


def research_warnings(brief: Brief) -> list[str]:
    warnings = ["Fixture research: no live retrieval or independent fact-check was performed."]
    for claim in brief.research.claims:
        if claim.status != "supported":
            warnings.append(f"{claim.id}: {claim.status}. {claim.caveat}")
    if brief.research.ranking:
        warnings.append(f"Ranking limitation: {brief.research.ranking.limitations}")
    return warnings


def audio_warnings(assets) -> list[str]:
    return [
        f"{a.kind} {a.artifact.id}: UNKNOWN — DO NOT PUBLISH"
        for a in assets
        if a.artifact.provenance.license_category == "unknown"
    ]
