"""
Drift guard for the object-type code set.

OBJ_TYPES (PiFinder.obj_types) is the single source of truth for the type codes.
The filter menu and the filter default are generated from it, so they can't
drift -- but two descriptions can't import Python and must be kept honest here:

- the "Object type codes" table in the obslist-formats README, and
- default_config.json, which must NOT re-hardcode the set.
"""

import json
import re
from pathlib import Path

import pytest

from PiFinder.obj_types import OBJ_TYPES

_ROOT = Path(__file__).resolve().parents[2]
_README = _ROOT / "docs/ax/catalog/obslist-formats/README.md"
_DEFAULT_CONFIG = _ROOT / "default_config.json"


def _readme_table_codes() -> set:
    """Extract the backticked codes from the README "Object type codes" table."""
    lines = _README.read_text().splitlines()
    codes: set = set()
    in_section = False
    for line in lines:
        if line.startswith("## "):
            in_section = line.strip() == "## Object type codes"
            continue
        if in_section and line.lstrip().startswith("|"):
            codes.update(re.findall(r"`([^`]+)`", line))
    return codes


@pytest.mark.unit
def test_readme_table_matches_obj_types():
    codes = _readme_table_codes()
    if not codes:
        # The "Object type codes" table lives in the obslist-formats docs change.
        # On a checkout that doesn't carry it yet there is nothing to guard; the
        # check activates once both land on main.
        pytest.skip("Object type codes table not present in this checkout")
    assert codes == set(OBJ_TYPES), (
        "The README 'Object type codes' table is out of sync with OBJ_TYPES."
    )


@pytest.mark.unit
def test_default_config_does_not_hardcode_object_types():
    config = json.loads(_DEFAULT_CONFIG.read_text())
    assert "filter.object_types" not in config, (
        "filter.object_types must default from OBJ_TYPES, not a hardcoded list "
        "in default_config.json."
    )
