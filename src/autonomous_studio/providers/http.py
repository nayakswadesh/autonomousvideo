"""Small injectable HTTPS transport. Errors intentionally exclude headers, URLs and bodies."""

import asyncio
import json
import urllib.error
import urllib.request
from time import perf_counter


class RemoteError(RuntimeError):
    def __init__(self, service, status=None, kind="network", provider_code=None):
        self.status, self.kind, self.provider_code = status, kind, provider_code
        super().__init__(
            f"{service} request failed"
            + (
                f" (HTTP {status}{': ' + provider_code if provider_code else ''})"
                if status
                else (" (timeout)" if kind == "timeout" else " (network)")
            )
        )


def _remote_error(service, exc):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return RemoteError(service, kind="timeout" if isinstance(reason, TimeoutError) else "network")


def _http_error(service, exc):
    """Keep only a bounded machine-readable error code; never expose response bodies."""
    try:
        body = json.loads(exc.read(4096))
        code = body.get("error", {}).get("code")
        if not isinstance(code, str) or len(code) > 64 or not code.replace("_", "").isalnum():
            code = None
    except (ValueError, TypeError, AttributeError):
        code = None
    return RemoteError(service, exc.code, provider_code=code)


class HTTP:
    async def get(self, url, headers=None, *, timeout=90, service="Provider", attempts=3):
        def execute():
            request = urllib.request.Request(url, headers=headers or {}, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.read(55_000_000)
            except urllib.error.HTTPError as exc:
                raise _http_error(service, exc) from None
            except (OSError, urllib.error.URLError) as exc:
                raise _remote_error(service, exc) from None

        for attempt in range(attempts):
            try:
                return await asyncio.to_thread(execute)
            except RemoteError as exc:
                if exc.status not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                    raise
                await asyncio.sleep(2**attempt)

    async def get_json(self, url, headers=None, **kwargs):
        return json.loads(await self.get(url, headers, **kwargs))

    async def post(
        self, url, body, headers=None, *, timeout=90, service="Provider", attempts=3,
        trace=None,
    ):
        def execute():
            request = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
            started = perf_counter()
            if trace is not None:
                trace["phase"] = "awaiting_headers"
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    if trace is None:
                        return response.read(55_000_000)
                    trace["seconds_to_headers"] = round(perf_counter() - started, 3)
                    trace["server_request_id"] = response.headers.get("x-request-id")
                    trace["processing_ms"] = response.headers.get("openai-processing-ms")
                    trace["phase"] = "awaiting_first_byte"
                    first = response.read(1)
                    trace["seconds_to_first_byte"] = round(perf_counter() - started, 3)
                    trace["phase"] = "reading_body"
                    chunks = [first] if first else []
                    received = len(first)
                    trace["bytes_received"] = received
                    while received < 55_000_000:
                        chunk = response.read(min(65536, 55_000_000 - received))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        received += len(chunk)
                        trace["bytes_received"] = received
                    trace["phase"] = "complete"
                    return b"".join(chunks)
            except urllib.error.HTTPError as exc:
                if trace is not None:
                    trace["failed_phase"] = trace["phase"]
                    trace["phase"] = "http_error"
                    trace["http_status"] = exc.code
                    trace["server_request_id"] = (
                        exc.headers.get("x-request-id") if exc.headers else None
                    )
                raise _http_error(service, exc) from None
            except (OSError, urllib.error.URLError) as exc:
                if trace is not None:
                    trace["failed_phase"] = trace["phase"]
                    trace["phase"] = "transport_error"
                    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                    trace["exception_type"] = type(reason).__name__
                raise _remote_error(service, exc) from None

        # Only explicit rate-limit/server responses are retried. An ambiguous network failure
        # may already have incurred a charge or delivered a message.
        for attempt in range(attempts):
            try:
                return await asyncio.to_thread(execute)
            except RemoteError as exc:
                if exc.status not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                    raise
                await asyncio.sleep(2**attempt)

    async def json(self, url, payload, headers=None, **kwargs):
        return json.loads(
            await self.post(
                url,
                json.dumps(payload).encode(),
                {"Content-Type": "application/json", **(headers or {})},
                **kwargs,
            )
        )
