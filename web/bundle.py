"""Unpack and repack the page's embedded application document.

The published page is a bundler wrapper: line for line it is a loader, the React and Babel
runtimes, and one `<script type="__bundler/template">` holding the entire application as a
JSON-encoded HTML document. Editing that string by hand is how a 300 kilobyte file gets
corrupted, so this does the round trip instead and refuses to write anything it cannot
reproduce byte for byte.

    python web/bundle.py unpack    ->  web/src/app.html
    python web/bundle.py pack      ->  web/index.html

The one subtlety is `/`. The document contains `</script>` tags, and a literal one inside a
JSON string inside a `<script>` element ends that element early and breaks the page. The
original escapes them as `\\u002F`, which is valid JSON and invisible to the parser, so the
repack does the same rather than hoping no closing tag ever appears.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BUNDLE = Path("web/index.html")
SOURCE = Path("web/src/app.html")
OPEN = '  <script type="__bundler/template">\n'


def _split(text: str) -> tuple[str, str, str]:
    start = text.index(OPEN) + len(OPEN)
    end = text.index("\n  </script>", start)
    return text[:start], text[start:end], text[end:]


def unpack() -> None:
    head, payload, tail = _split(BUNDLE.read_text(encoding="utf-8"))
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    SOURCE.write_text(json.loads(payload), encoding="utf-8")
    _ = head, tail
    print(f"{SOURCE} written, {SOURCE.stat().st_size} bytes")


def _encode(document: str) -> str:
    return json.dumps(document, ensure_ascii=False).replace("</", "<\\u002F")


def pack() -> None:
    text = BUNDLE.read_text(encoding="utf-8")
    head, payload, tail = _split(text)
    document = SOURCE.read_text(encoding="utf-8")
    if json.loads(_encode(document)) != document:
        raise SystemExit("the encoded document does not decode back to itself; refusing to write")
    BUNDLE.write_text(head + _encode(document) + tail, encoding="utf-8")
    print(f"{BUNDLE} written, {BUNDLE.stat().st_size} bytes")
    _ = payload


if __name__ == "__main__":
    {"unpack": unpack, "pack": pack}[sys.argv[1]]()
