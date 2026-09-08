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

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "index.html"
#: The islands, in the order they are appended. Each is wrapped in its own markers so a
#: re-run replaces it rather than adding a second copy.
PANELS = [
    ("the settlement panel", HERE / "run-panel.html", "run-panel"),
    ("the simulator", HERE / "sim-panel.html", "sim-panel"),
]
SOURCE = HERE / "src" / "app.html"

#: Edits to the application source, kept as data rather than as Python string literals.
#: They contain JavaScript with quotes, braces and newlines, and embedding that in source
#: is how an escaping mistake becomes a corrupted bundle nobody can read back.
EDITS = HERE / "source-edits.json"


#: Each entry is (name, pattern, replacement, already). `pattern` is a regular expression
#: because an exact literal broke on the first re-export it met: the design changed the shell's
#: top padding from `14px` to `0`, which has nothing to do with what this override is for, and
#: the anchor vanished. `already` is what the applied result looks like, so a second run is a
#: no-op rather than a failure.
OVERRIDES = [
    (
        "the shell fills the screen",
        # The bundle centres everything in 1180px, which leaves most of a wide monitor empty.
        # Only the shell is widened. The prose measures inside it, 34ch through 82ch, are
        # deliberate and stay: a paragraph running the full width of a 2000px display is
        # unreadable, and widening those would trade one bad layout for another.
        # Three values: top, horizontal, bottom. Only the horizontal one is replaced, so the
        # design keeps whatever top and bottom padding it chose. A greedier capture swallowed
        # both of the first two and produced a four-value padding, which is a different rule.
        r"max-width:1180px;margin:0 auto;padding:(\S+) \S+ 96px",
        r"max-width:none;margin:0 auto;padding:\1 clamp(14px,3vw,44px) 96px",
        "max-width:none;margin:0 auto;padding:",
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

    # Teach the design's own simulator to ask the engine instead of replaying a fixture.
    # Each edit is checked for exactly one match, because a silently skipped one would leave a
    # simulator that looks wired and is not, which is worse than one that is obviously stale.
    for edit in json.loads(EDITS.read_text(encoding="utf-8")):
        if edit["new"] in document:
            print(f"  already applied: {edit['name']}")
            continue
        if document.count(edit["old"]) != 1:
            raise SystemExit(
                f"  ANCHOR LOST: {edit['name']!r} matched {document.count(edit['old'])} times, "
                "not once. The export changed; rewrite this edit rather than skipping it."
            )
        document = document.replace(edit["old"], edit["new"], 1)
        print(f"  applied: {edit['name']}")

    if "</body>" not in document:
        raise SystemExit("the exported document has no </body>; the panels have nowhere to go")

    for name, source, marker in PANELS:
        start, end = f"<!-- wrasse:{marker}:start -->", f"<!-- wrasse:{marker}:end -->"
        # The trailing newline is part of what gets removed. Without it every run left one
        # behind and the file grew a byte at a time, which is not idempotent even though it
        # looked like it: the panel count stayed at one and only the whitespace drifted.
        document = re.sub(
            re.escape(start) + ".*?" + re.escape(end) + r"\n?", "", document, flags=re.S
        )
        block = f"{start}\n{source.read_text(encoding='utf-8').rstrip()}\n{end}\n"
        document = document.replace("</body>", block + "</body>", 1)
        print(f"  applied: {name}")

    SOURCE.write_text(document, encoding="utf-8")
    _bundle("pack")


def main() -> int:
    if not PAGE.is_file():
        print(f"{PAGE} is missing", file=sys.stderr)
        return 1

    text = PAGE.read_text(encoding="utf-8")
    for name, pattern, replacement, already in OVERRIDES:
        if already in text:
            print(f"  already applied: {name}")
            continue
        text, count = re.subn(pattern, replacement, text, count=1)
        if count != 1:
            print(
                f"  ANCHOR LOST: {name!r} matched {count} times, not once. "
                "The export changed; rewrite this override rather than skipping it.",
                file=sys.stderr,
            )
            return 1
        print(f"  applied: {name}")

    PAGE.write_text(text, encoding="utf-8")

    # After the text overrides, because it round-trips the whole file and would otherwise be
    # rewriting a document the overrides had not been applied to yet.
    inject_panel()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
