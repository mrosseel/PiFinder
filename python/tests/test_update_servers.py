"""python/PiFinder/update_servers.json is the one place that names the
update servers. The code reads it; only the CI workflows keep a literal copy
(see the note in nixos-pr-build.yml), and that copy must match it."""

import importlib.util
import re
from pathlib import Path

import pytest

from PiFinder import nixos_upgrade, update_servers

REPO = Path(__file__).resolve().parents[2]

# Places with code or config. Docs (ADRs, CONTEXT.md, .rst) may name the
# servers in prose.
SCANNED = [
    REPO / "flake.nix",
    REPO / "nixos",
    REPO / ".github",
    REPO / "python" / "PiFinder",
]
SUFFIXES = {".nix", ".py", ".sh", ".yml", ".yaml", ".toml"}
WORKFLOWS = REPO / ".github" / "workflows"
SERVER_URL = re.compile(r"https://(?:cache|deltas)\.pifinder\.[a-z]+(?:/[\w-]+)?")


def _scanned_files():
    for root in SCANNED:
        files = [root] if root.is_file() else root.rglob("*")
        for path in files:
            if path.suffix in SUFFIXES and "sync-conflict" not in path.name:
                yield path


@pytest.mark.unit
def test_servers_are_https_without_trailing_slash():
    for url in (
        update_servers.ATTIC_CACHE,
        update_servers.DEV_CACHE,
        update_servers.RELEASE_CACHE,
        update_servers.DELTA_SERVER,
    ):
        assert url.startswith("https://")
        assert not url.endswith("/")


@pytest.mark.unit
def test_upgrade_uses_the_shared_caches():
    assert nixos_upgrade.CACHES == (
        update_servers.DEV_CACHE,
        update_servers.RELEASE_CACHE,
    )


@pytest.mark.unit
def test_update_manifest_reads_the_same_file():
    spec = importlib.util.spec_from_file_location(
        "update_manifest", REPO / ".github" / "scripts" / "update_manifest.py"
    )
    assert spec is not None and spec.loader is not None
    update_manifest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(update_manifest)
    assert update_manifest.DEV_CACHE == update_servers.DEV_CACHE
    assert update_manifest.RELEASE_CACHE == update_servers.RELEASE_CACHE


@pytest.mark.unit
def test_no_other_file_names_the_servers():
    found = [
        f"{path.relative_to(REPO)}: {url}"
        for path in _scanned_files()
        if WORKFLOWS not in path.parents
        for url in SERVER_URL.findall(path.read_text(errors="replace"))
    ]
    assert found == [], "use update_servers.json (or update-servers.nix)"


@pytest.mark.unit
def test_workflow_copy_matches_the_file():
    allowed = {
        update_servers.ATTIC_CACHE,
        update_servers.DEV_CACHE,
        update_servers.RELEASE_CACHE,
    }
    found = {
        url
        for path in WORKFLOWS.glob("*.yml")
        for url in SERVER_URL.findall(path.read_text())
    }
    assert found, "the workflows log in to the Attic cache"
    assert found <= allowed, found - allowed
