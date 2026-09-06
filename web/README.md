# The page

Claude Design's HTML goes here as `index.html`. The service serves this directory at `/`, on
the same origin as the API, so the page calls `/api/quote` with no CORS and one link carries
both halves of the demo.

    web/index.html      the page
    /api/quote          the document it renders
    /api/health         enough to tell a deploy from a corpse

Anything else the page needs, a font or an image, sits beside it and is served from the same
place. Keep it to files: nothing here is built, and a build step is one more thing that can be
broken at the wrong moment.

If this directory holds no `index.html` the service still answers the API. That is deliberate.
The page is a view of the quote, not the other way round.

## Re-exporting the page

`index.html` is a generated bundle and we change two things in it. Those changes live in
`apply-overrides.py`, not in the file, because a hand edit to a generated file disappears
silently on the next export.

    cp "/mnt/c/Users/hr/Downloads/Wrasse quote.html" web/index.html
    python3 web/apply-overrides.py

The script is idempotent and fails loudly if an anchor no longer matches, which means the
export changed underneath it and the override needs rewriting rather than skipping.
