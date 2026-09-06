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

import sys
from pathlib import Path

PAGE = Path(__file__).resolve().parent / "index.html"

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
