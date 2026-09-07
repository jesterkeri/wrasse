#!/usr/bin/env python3
"""Re-apply local changes to a fresh export from Claude Design.

`index.html` is a generated bundle. Editing it by hand works and is lost the next time the
page is exported, silently, which is the worst way for a fix to disappear. Every change we
make to it lives here instead, so a fresh export is one command away from correct:

    cp "/mnt/c/Users/hr/Downloads/Wrasse quote.html" web/index.html
    python3 web/apply-overrides.py

Each override is idempotent and fails loudly if its anchor is gone, because an anchor that
stopped matching means the export changed underneath it and the override needs rewriting
rather than skipping.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "index.html"
PANEL = HERE / "run-panel.html"
SOURCE = HERE / "src" / "app.html"

#: The panel is wrapped in these so re-running replaces it rather than appending a second copy.
#: Editing `run-panel.html` and running this again is the whole update path.
START = "<!-- wrasse:run-panel:start -->"
END = "<!-- wrasse:run-panel:end -->"

OVERRIDES = [
    (
        "the shell fills the screen",
        # The bundle centres everything in 1180px, which leaves most of a wide monitor empty.
        # Only the shell is widened. The prose measures inside it, 34ch through 82ch, are
        # deliberate and stay: a paragraph running the full width of a 2000px display is
        # unreadable, and widening those would trade one bad layout for another.
        'max-width:1180px;margin:0 auto;padding:14px 14px 96px',
        'max-width:none;margin:0 auto;padding:14px clamp(14px,3vw,44px) 96px',
    ),
]


def _bundle(action: str) -> None:
    """Unpack or repack through `bundle.py`, which checks its own round trip."""

    subprocess.run(
        [sys.executable, str(HERE / "bundle.py"), action],
        check=True, cwd=HERE.parent, stdout=subprocess.DEVNULL,
    )


def inject_panel() -> None:
    """Put the settlement panel into the application document, replacing any earlier copy.

    It goes inside the embedded template rather than beside it in the outer bundle, because the
    outer document is what the loader rewrites. Within that document it is a sibling of
    `<x-dc>`, never a child: the design runtime owns that element and re-renders it, so a panel
    inside would be erased the first time anything changed.
    """

    _bundle("unpack")
    document = SOURCE.read_text(encoding="utf-8")
    # The trailing newline is part of what gets removed. Without it every run left one
    # behind and the file grew a byte at a time, which is not idempotent even though it
    # looked like it: the panel count stayed at one and only the whitespace drifted.
    document = re.sub(
        re.escape(START) + ".*?" + re.escape(END) + r"\n?", "", document, flags=re.S
    )

    block = f"{START}\n{PANEL.read_text(encoding='utf-8').rstrip()}\n{END}\n"
    if "</body>" not in document:
        raise SystemExit("the exported document has no </body>; the panel has nowhere to go")
    document = document.replace("</body>", block + "</body>", 1)
    SOURCE.write_text(document, encoding="utf-8")
    _bundle("pack")
    print("  applied: the settlement panel")


def main() -> int:
    if not PAGE.is_file():
        print(f"{PAGE} is missing", file=sys.stderr)
        return 1

    text = PAGE.read_text(encoding="utf-8")
    for name, old, new in OVERRIDES:
        if new in text:
            print(f"  already applied: {name}")
            continue
        if text.count(old) != 1:
            print(
                f"  ANCHOR LOST: {name!r} expected exactly one match, found {text.count(old)}. "
                "The export changed; rewrite this override rather than skipping it.",
                file=sys.stderr,
            )
            return 1
        text = text.replace(old, new, 1)
        print(f"  applied: {name}")

    PAGE.write_text(text, encoding="utf-8")

    # After the text overrides, because it round-trips the whole file and would otherwise be
    # rewriting a document the overrides had not been applied to yet.
    inject_panel()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
