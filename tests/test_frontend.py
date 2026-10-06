"""The frontend's pure geometry has node tests (tests/frontend); run them from
pytest when node is available, and syntax-check every panel module."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_frontend_node_tests_pass():
    # Files, not the directory: Node before 23 does not expand a directory for --test.
    files = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "tests" / "frontend").glob("*.test.mjs"))
    run = subprocess.run([NODE, "--test", *files], cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_every_frontend_module_parses():
    for path in sorted((ROOT / "custom_components" / "sextant" / "frontend").glob("sextant-*.js")):
        run = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True)
        assert run.returncode == 0, f"{path.name}: {run.stderr}"
