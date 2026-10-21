import asyncio
import base64
import io
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from PIL import Image

from autonomous_studio.config import Settings
from autonomous_studio.domain.models import (
    AuthenticMediaCandidate,
    GeneratedMediaCandidate,
    LongFormVisualPolicy,
)
from autonomous_studio.providers.http import RemoteError, _http_error
from autonomous_studio.providers.longform_media import _commons_image, _openai_image
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"


class RateLimitedImages:
    def __init__(self, content, error_code=None):
        self.calls = 0
        self.content = content
        self.error_code = error_code

    async def json(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RemoteError("OpenAI Images", 429, provider_code=self.error_code)
        return {"data": [{"b64_json": base64.b64encode(self.content).decode()}]}


def test_explicit_image_rate_limit_retries_without_another_budget_slot(tmp_path, monkeypatch):
    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE))
    project.longform_visual_policy = LongFormVisualPolicy()
    asset = GeneratedMediaCandidate(
        id="image_a",
        beat_ids=["beat_a"],
        prompt="A detailed scientific illustration with a cinematic but factual composition",
    )
    source = Image.new("RGB", (1536, 1024), "#123456")
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")
    http = RateLimitedImages(buffer.getvalue())
    studio.live_client = SimpleNamespace(
        settings=Settings(api_key="test"), cache=tmp_path / "cache", http=http
    )

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr("autonomous_studio.providers.longform_media.asyncio.sleep", no_delay)
    output = tmp_path / "output.png"
    asyncio.run(_openai_image(studio, project, asset, output))
    assert http.calls == 2
    assert project.longform_image_requests == 1
    assert output.is_file()

    # A restart reuses the paid image receipt, never sending another generation request.
    asyncio.run(_openai_image(studio, project, asset, output))
    assert http.calls == 2
    assert project.longform_image_requests == 1


def test_http_error_reports_only_safe_provider_code():
    response = io.BytesIO(
        b'{"error":{"code":"insufficient_quota","message":"private account detail"}}'
    )
    error = HTTPError("https://example.invalid", 429, "rate limited", {}, response)
    remote = _http_error("OpenAI Images", error)
    assert remote.provider_code == "insufficient_quota"
    assert "private account detail" not in str(remote)


def test_exhausted_credits_pause_without_backoff_and_resume_same_reservation(tmp_path, monkeypatch):
    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE))
    project.longform_visual_policy = LongFormVisualPolicy()
    asset = GeneratedMediaCandidate(
        id="image_a",
        beat_ids=["beat_a"],
        prompt="A detailed scientific illustration with a cinematic but factual composition",
    )
    source = Image.new("RGB", (1536, 1024), "#123456")
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")
    http = RateLimitedImages(buffer.getvalue(), error_code="credit_balance_exhausted")
    studio.live_client = SimpleNamespace(
        settings=Settings(api_key="test"), cache=tmp_path / "cache", http=http
    )

    async def unexpected_delay(_seconds):
        raise AssertionError("credit balance failure must not back off and retry")

    monkeypatch.setattr(
        "autonomous_studio.providers.longform_media.asyncio.sleep", unexpected_delay
    )
    output = tmp_path / "output.png"
    with pytest.raises(ValueError, match="credit balance is exhausted"):
        asyncio.run(_openai_image(studio, project, asset, output))
    assert http.calls == 1
    assert project.longform_image_requests == 1
    asyncio.run(_openai_image(studio, project, asset, output))
    assert http.calls == 2
    assert project.longform_image_requests == 1
    assert output.is_file()


def test_image_service_failure_does_not_create_a_code_rendered_substitute(tmp_path):
    class FailingImages:
        async def json(self, *_args, **_kwargs):
            raise RemoteError("OpenAI Images", 503)

    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE))
    project.longform_visual_policy = LongFormVisualPolicy()
    asset = GeneratedMediaCandidate(
        id="image_failure",
        beat_ids=["beat_failure"],
        prompt="A clear editorial illustration of the narrated scientific process",
    )
    studio.live_client = SimpleNamespace(
        settings=Settings(api_key="test"), cache=tmp_path / "cache", http=FailingImages()
    )
    output = tmp_path / "failed.png"
    with pytest.raises(RemoteError, match="OpenAI Images"):
        asyncio.run(_openai_image(studio, project, asset, output))
    assert not output.exists()


def test_moderation_rejection_identifies_asset_and_blocks_unchanged_retry(tmp_path):
    class BlockedImages:
        def __init__(self):
            self.calls = 0

        async def json(self, *_args, **_kwargs):
            self.calls += 1
            raise RemoteError("OpenAI Images", 400, provider_code="moderation_blocked")

    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE))
    project.longform_visual_policy = LongFormVisualPolicy()
    asset = GeneratedMediaCandidate(
        id="image_sensitive",
        beat_ids=["beat_sensitive"],
        prompt="A proposed editorial illustration",
    )
    http = BlockedImages()
    studio.live_client = SimpleNamespace(
        settings=Settings(api_key="test"), cache=tmp_path / "cache", http=http
    )
    output = tmp_path / "blocked.png"
    for _ in range(2):
        with pytest.raises(ValueError, match="image_sensitive.*revise this visual request"):
            asyncio.run(_openai_image(studio, project, asset, output))
    assert http.calls == 1
    assert project.longform_image_requests == 1
    assert not output.exists()


@pytest.mark.parametrize("host,allowed", [("thumb.wikimedia.org", True), ("other.invalid", False)])
def test_commons_media_allows_only_exact_approved_wikimedia_hosts(tmp_path, host, allowed):
    source = Image.new("RGB", (500, 500), "#778899")
    buffer = io.BytesIO()
    source.save(buffer, format="JPEG")
    page = "https://commons.wikimedia.org/wiki/File:Test.jpg"
    asset = AuthenticMediaCandidate(
        id="photo_a",
        beat_ids=["beat_a"],
        search_query="historical image",
        title="Test photo",
        creator="Test author",
        source_page=page,
        preview_url="https://upload.wikimedia.org/test.jpg",
        license_name="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution="Test author, CC BY 4.0",
    )

    class CommonsHTTP:
        async def get_json(self, *_args, **_kwargs):
            return {
                "query": {
                    "pages": [
                        {
                            "imageinfo": [
                                {
                                    "thumburl": f"https://{host}/test.jpg",
                                    "descriptionurl": page,
                                    "mime": "image/jpeg",
                                    "extmetadata": {
                                        "LicenseShortName": {"value": "CC BY 4.0"},
                                        "LicenseUrl": {
                                            "value": "https://creativecommons.org/licenses/by/4.0/"
                                        },
                                    },
                                }
                            ]
                        }
                    ]
                }
            }

        async def get(self, *_args, **_kwargs):
            return buffer.getvalue()

    studio = SimpleNamespace(live_client=SimpleNamespace(http=CommonsHTTP()))
    output = tmp_path / "commons.png"
    if allowed:
        asyncio.run(_commons_image(studio, asset, output))
        assert output.is_file()
    else:
        with pytest.raises(ValueError, match="media URL is invalid"):
            asyncio.run(_commons_image(studio, asset, output))
