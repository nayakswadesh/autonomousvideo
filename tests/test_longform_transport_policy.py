"""Transient provider errors are transport retries, not creative redrafts."""

import asyncio
import io
import urllib.error

import pytest

from autonomous_studio.providers.http import HTTP, RemoteError


def test_503_retries_transport_without_requesting_a_new_creative_draft(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, *_args):
            return b'{"status":"completed"}'

    def urlopen_valid(_request, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                "https://api.openai.com/v1/responses", 503,
                "Service Unavailable", None,
                io.BytesIO(b'{"error":{"code":"service_unavailable"}}'),
            )
        return Response()

    async def no_delay(_seconds):
        pass

    monkeypatch.setattr("urllib.request.urlopen", urlopen_valid)
    monkeypatch.setattr("autonomous_studio.providers.http.asyncio.sleep", no_delay)
    result = asyncio.run(HTTP().post(
        "https://api.openai.com/v1/responses", b"same payload", attempts=2
    ))
    assert result == b'{"status":"completed"}'
    assert len(calls) == 2


def test_speech_transport_trace_records_response_phases_without_payload(monkeypatch):
    class Response:
        headers = {"x-request-id": "req-example", "openai-processing-ms": "42"}

        def __init__(self):
            self.remaining = b"IFFaudio"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, amount):
            if amount == 1:
                return b"R"
            result, self.remaining = self.remaining, b""
            return result

    def urlopen_valid(request, timeout):
        assert timeout == 90
        assert any(
            key.lower() == "x-client-request-id" and value == "studio-tts-example"
            for key, value in request.headers.items()
        )
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen_valid)
    trace = {}
    result = asyncio.run(HTTP().post(
        "https://api.openai.com/v1/audio/speech",
        b"private narration",
        {"Authorization": "Bearer private-key", "X-Client-Request-Id": "studio-tts-example"},
        service="OpenAI TTS",
        trace=trace,
    ))
    assert result == b"RIFFaudio"
    assert trace["phase"] == "complete"
    assert trace["server_request_id"] == "req-example"
    assert trace["processing_ms"] == "42"
    assert trace["bytes_received"] == len(result)
    assert trace["seconds_to_headers"] <= trace["seconds_to_first_byte"]
    assert "private" not in str(trace)


def test_speech_transport_trace_distinguishes_timeout_phase(monkeypatch):
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, amount):
            if amount == 1:
                return b"R"
            raise TimeoutError("private proxy address")

    monkeypatch.setattr("urllib.request.urlopen", lambda _request, timeout: Response())
    trace = {}
    with pytest.raises(RemoteError, match="timeout"):
        asyncio.run(HTTP().post(
            "https://api.openai.com/v1/audio/speech", b"private narration",
            service="OpenAI TTS", trace=trace,
        ))
    assert trace["phase"] == "transport_error"
    assert trace["failed_phase"] == "reading_body"
    assert trace["bytes_received"] == 1
    assert trace["exception_type"] == "TimeoutError"
    assert "private" not in str(trace)
