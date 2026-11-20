"""The PiFinder update servers: the Attic cache and the delta server.

update_servers.json beside this module is the one place that names them.
Nix reads the same file (nixos/update-servers.nix) for the substituters and
pifinder.deltaUrl, and so does .github/scripts/update_manifest.py. The CI
workflows keep their own copy for the Attic login, because the PR build runs
the PR's code with the Attic token; test_update_servers.py checks that the
copy matches this file.
"""

import json
from pathlib import Path

SERVERS_FILE = Path(__file__).with_name("update_servers.json")

_servers = json.loads(SERVERS_FILE.read_text())

ATTIC_CACHE: str = _servers["attic_cache"]
DEV_CACHE: str = f"{ATTIC_CACHE}/{_servers['dev_namespace']}"
RELEASE_CACHE: str = f"{ATTIC_CACHE}/{_servers['release_namespace']}"
DELTA_SERVER: str = _servers["delta_server"]
