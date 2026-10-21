"""Import the pinned Phosphor regular SVG family for offline semantic lookup."""

from __future__ import annotations

import io
import urllib.request
import zipfile
from pathlib import Path

REVISION = "2b75f3ad12b420c9504ef05df8d2564a28f8500e"
ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "src/autonomous_studio/assets/semantic/phosphor/regular"


def main():
    url = f"https://codeload.github.com/phosphor-icons/core/zip/{REVISION}"
    with urllib.request.urlopen(url, timeout=90) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    paths = [
        item
        for item in archive.infolist()
        if "/assets/regular/" in item.filename and item.filename.endswith(".svg")
    ]
    if len(paths) < 1000:
        raise RuntimeError(f"Unexpected Phosphor SVG count: {len(paths)}")
    DEST.mkdir(parents=True, exist_ok=True)
    for item in paths:
        name = Path(item.filename).name
        if not name or name in (".", ".."):
            raise ValueError(f"Invalid source name: {item.filename}")
        target = DEST / name
        content = archive.read(item)
        if target.exists():
            if target.read_bytes() != content:
                raise RuntimeError(f"Existing SVG differs from pinned source: {name}")
            continue
        target.write_bytes(content)
    print(f"Imported {len(paths)} Phosphor SVGs from {REVISION}")


if __name__ == "__main__":
    main()
