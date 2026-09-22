"""Frontend assets reference no external host. The sovereignty claim covers the whole
system an engineer touches, not just the API process -- a `<script src="https://cdn...">`
in the UI is an external call the network monitor (Expected Solution, problem statement)
would have to explain away, so it cannot exist at all.

Grep-based (a URL is a string pattern, not a syntax tree): scans web/ for
`http(s)://` occurrences and flags any host outside a small localhost/relative
allowlist. `web/` has no built assets yet (M1+ work) -- on a clean tree this test
passes because there is nothing to scan, not because it has been exercised against
real frontend code. The negative control is what proves the mechanism works today,
ready for the moment web/ has real files.
"""

from __future__ import annotations

import re

from conftest import REPO_ROOT, control

_URL_RE = re.compile(r'https?://([^\s"\'<>/]+)')
_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "host.docker.internal"})
_ASSET_GLOBS = ("*.html", "*.js", "*.jsx", "*.ts", "*.tsx", "*.css")


def _external_urls(text: str) -> list[str]:
    hits = []
    for match in _URL_RE.finditer(text):
        host = match.group(1).split(":")[0]  # strip a port, if any
        if host not in _ALLOWED_HOSTS:
            hits.append(match.group(0))
    return hits


def test_no_web_asset_references_an_external_host():
    web_root = REPO_ROOT / "web"
    offenders = []
    if web_root.exists():
        for pattern in _ASSET_GLOBS:
            for path in web_root.rglob(pattern):
                if "node_modules" in path.parts or "dist" in path.parts or "build" in path.parts:
                    continue
                hits = _external_urls(path.read_text(encoding="utf-8"))
                if hits:
                    offenders.append(f"{path}: {hits}")

    assert offenders == [], "external host referenced in a web asset: " + "; ".join(offenders)


def test_the_detector_catches_the_negative_control():
    hits = _external_urls(control("external_cdn_url"))
    assert len(hits) == 2
    assert any("cdn.jsdelivr.net" in h for h in hits)
    assert any("cdnjs.cloudflare.com" in h for h in hits)


def test_the_detector_is_not_vacuous():
    assert _external_urls('<script src="/static/app.js"></script>') == []
    assert _external_urls('fetch("http://localhost:8000/api/tasks")') == []
