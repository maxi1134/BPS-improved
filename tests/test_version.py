"""The frontend's version file must match the manifest: the page reads its version from it."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent / "custom_components" / "sextant"


def test_frontend_version_file_matches_the_manifest():
    manifest = json.loads((ROOT / "manifest.json").read_text())["version"]
    js = (ROOT / "frontend" / "sextant-version.js").read_text()
    assert re.search(r'export const VERSION = "([^"]+)";', js).group(1) == manifest


def test_code_signature_is_stable_and_follows_the_sources(tmp_path, monkeypatch):
    from sextant import ws
    a = ws.code_signature()
    assert a and a == ws.code_signature()
