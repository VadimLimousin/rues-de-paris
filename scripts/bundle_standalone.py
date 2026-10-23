#!/usr/bin/env python3
"""
Builds a single self-contained HTML file for sharing — inlines style.css,
data/paris_streets.js, and game.js directly into index.html, so there are
no sibling files for a recipient's OS/browser to lose track of. This is
what makes the game reliably openable on Android Chrome (which blocks a
standalone local HTML file from loading sibling files via file://) and
immune to the "opened one file out of a zip without extracting" mistake
on any platform.

The real source files (index.html, style.css, game.js,
data/paris_streets.js) are untouched — keep developing against those.
Re-run this after any change you want reflected in the shared file.

Run: python3 scripts/bundle_standalone.py
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
OUT = os.path.join(ROOT, "rues-de-paris-standalone.html")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


def main():
    html = read("index.html")
    style_css = read("style.css")
    paris_streets_js = read("data/paris_streets.js")
    game_js = read("game.js")

    # Matches regardless of a "?v=N" cache-busting suffix on the tag, so
    # this doesn't need editing every time that version number is bumped.
    html = re.sub(
        r'<link rel="stylesheet" href="style\.css(?:\?[^"]*)?">',
        lambda m: "<style>\n" + style_css + "\n</style>",
        html,
    )
    html = re.sub(
        r'<script src="data/paris_streets\.js(?:\?[^"]*)?"></script>',
        lambda m: "<script>\n" + paris_streets_js + "\n</script>",
        html,
    )
    html = re.sub(
        r'<script src="game\.js(?:\?[^"]*)?"></script>',
        lambda m: "<script>\n" + game_js + "\n</script>",
        html,
    )

    remaining = re.findall(r'<(?:script src|link rel="stylesheet" href)="([^"]+)"', html)
    local_remaining = [r for r in remaining if not r.startswith("http")]
    if local_remaining:
        raise RuntimeError(f"Still referencing local files, bundling incomplete: {local_remaining}")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {OUT} ({os.path.getsize(OUT) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
