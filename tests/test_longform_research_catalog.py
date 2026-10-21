import asyncio
import json
from pathlib import Path

import pytest

from autonomous_studio.config import Settings
from autonomous_studio.domain.longform_policy import LongFormCreativePolicy
from autonomous_studio.providers.longform import (
    LongFormResearchConversionDraft,
    MockLongFormResearch,
    OpenAILongFormResearch,
    materialize_research_brief,
    observed_source_catalog,
    research_conversion_schema,
)
from autonomous_studio.providers.openai import OpenAIClient, ProviderValidationError
from autonomous_studio.workflow.engine import load_brief

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"
URL_A = "https://example.org/a-formation"
URL_B = "https://example.org/b-evolution"


def completed(text, *, urls=(), annotations=()):
    return {
        "status": "completed",
        "output": [
            {"type": "web_search_call", "action": {"sources": [{"url": url} for url in urls]}},
            {"type": "message", "content": [{
                "type": "output_text", "text": text, "annotations": list(annotations),
            }]},
        ],
    }


class FakeHTTP:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def json(self, url, payload, headers=None, **kwargs):
        self.calls.append(payload)
        return self.replies.pop(0)


def conversion_payload(source_ids=("s1", "s2")):
    return {
        "requirements": ["Preserve uncertainty."],
        "ranking": None,
        "subjects": [],
        "claims": [
            {
                "id": "c7", "kind": "qualitative", "text": "The star expands.",
                "source_ids": list(source_ids), "confidence": 0.8,
                "status": "supported", "caveat": "Model-based prediction.",
            }
        ],
        "source_notes": [
            {"source_id": "s1", "evidence_notes": "Supports the formation account."}
        ],
    }


def test_catalog_assigns_ids_to_observed_urls_and_uses_tool_titles():
    response = completed(
        "Evidence about the future of the star. [source]",
        urls=(URL_B, URL_A),
        annotations=({
            "type": "url_citation", "url": URL_B, "title": "Stellar evolution",
            "start_index": 35, "end_index": 43,
        },),
    )
    catalog = observed_source_catalog(response)
    assert [(source.id, source.url) for source in catalog] == [
        ("s1", URL_A), ("s2", URL_B)
    ]
    assert catalog[1].title == "Stellar evolution"


def test_dynamic_schema_constrains_all_claim_references_to_catalog():
    schema = research_conversion_schema(["s1", "s2"])
    for kind in ("NumericClaim", "QualitativeClaim"):
        assert schema["$defs"][kind]["properties"]["source_ids"]["items"]["enum"] == [
            "s1", "s2"
        ]
    assert schema["$defs"]["ResearchSourceNote"]["properties"]["source_id"]["enum"] == [
        "s1", "s2"
    ]
    assert "sources" not in schema["properties"]


def test_materialization_never_creates_orphaned_source_ids():
    catalog = observed_source_catalog(completed("evidence", urls=(URL_A, URL_B)))
    valid = LongFormResearchConversionDraft.model_validate(conversion_payload())
    research = materialize_research_brief(valid, catalog)
    assert [source.id for source in research.sources] == ["s1", "s2"]
    assert research.sources[0].evidence_notes == "Supports the formation account."
    assert all(set(claim.source_ids) <= {source.id for source in research.sources}
               for claim in research.claims)

    invalid = LongFormResearchConversionDraft.model_validate(
        conversion_payload(("s1", "s6"))
    )
    with pytest.raises(ValueError, match="claim c7 cites outside"):
        materialize_research_brief(invalid, catalog)


def test_research_conversion_uses_same_luna_call_and_fails_closed_on_orphan(tmp_path):
    brief = load_brief(EXAMPLE)
    plan = asyncio.run(MockLongFormResearch().plan(brief)).model_copy(
        update={"minimum_distinct_sources": 2}
    )
    raw = completed("A source-backed research summary.", urls=(URL_A, URL_B))
    http = FakeHTTP([raw, completed(json.dumps(conversion_payload()))])
    client = OpenAIClient(Settings(api_key="test"), tmp_path / "valid", http)
    client.longform_creative_policy = LongFormCreativePolicy()
    research = asyncio.run(OpenAILongFormResearch(client).research(brief, plan))
    assert len(http.calls) == 2
    assert all(call["model"] == "gpt-6-luna" for call in http.calls)
    assert [source.id for source in research.sources] == ["s1", "s2"]
    claim_schema = http.calls[1]["text"]["format"]["schema"]["$defs"]["QualitativeClaim"]
    assert claim_schema["properties"]["source_ids"]["items"]["enum"] == ["s1", "s2"]

    orphan_http = FakeHTTP([raw, completed(json.dumps(conversion_payload(("s1", "s6"))))])
    orphan_client = OpenAIClient(Settings(api_key="test"), tmp_path / "orphan", orphan_http)
    orphan_client.longform_creative_policy = LongFormCreativePolicy()
    with pytest.raises(ProviderValidationError, match="claim c7 cites outside"):
        asyncio.run(OpenAILongFormResearch(orphan_client).research(brief, plan))
    assert len(orphan_http.calls) == 2
