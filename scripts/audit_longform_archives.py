"""Read-only audit of a saved long-form visual draft's Commons requests."""

import asyncio
import json
import sys
from pathlib import Path

from autonomous_studio.providers.http import HTTP
from autonomous_studio.providers.longform_visuals import (
    AuthenticRequestDraft,
    WikimediaCommonsDiscovery,
)


async def main(path: Path) -> None:
    draft = json.loads(path.read_text(encoding="utf-8"))
    discovery = WikimediaCommonsDiscovery(HTTP())
    for item in draft["assets"]:
        if item["kind"] != "authentic_image":
            continue
        request = AuthenticRequestDraft.model_validate(item)
        try:
            candidate = await discovery.discover(request, None)
            print(request.id, candidate.title, candidate.license_name, candidate.source_page)
        except (ValueError, RuntimeError) as exc:
            print(request.id, type(exc).__name__, str(exc))
        await asyncio.sleep(0.75)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
