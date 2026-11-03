"""How far the running shell is from wizard-ux main, for Settings > About (wizard-ux#887 rule 5).

The shell sends the commit its image was built from (its build.json). This module asks GitHub's public compare
API with no token, so no credential leaves the box. Each answer is cached for an hour (a failure for ten minutes),
so About can be opened any number of times and the box still asks at most once an hour per commit.

    compare(commit) -> {"commit", "base", "behind", "own", "checked_at"}   base = the main commit it sits on
                    -> {"commit", "error"}                                 unknown commit / GitHub unreachable
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO = "humanitylabs-org/wizard-ux"
SHA = re.compile(r"[0-9a-f]{40}")
TTL, FAIL_TTL, MAX_BYTES = 3600.0, 600.0, 4 << 20
_cache: dict[str, tuple[float, dict]] = {}


def _fetch(url: str, timeout: float = 8) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "wizard-app-manager"})
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 - fixed https URL
        return json.loads(r.read(MAX_BYTES))


def compare(commit: str, fetch=None, now=time.monotonic) -> dict:
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise ValueError("commit must be a full 40-character lowercase SHA")
    hit = _cache.get(commit)
    if hit and now() < hit[0]:
        return hit[1]
    try:
        d = (fetch or _fetch)(f"https://api.github.com/repos/{REPO}/compare/{commit}...main?per_page=1")
        base = d["merge_base_commit"]["sha"]
        behind, own = d["ahead_by"], d["behind_by"]          # main's view: main is ahead of the box by `behind`
        if not (isinstance(base, str) and SHA.fullmatch(base) and type(behind) is int and type(own) is int
                and behind >= 0 and own >= 0):
            raise ValueError("unexpected compare answer")
        res, ttl = {"commit": commit, "base": base, "behind": behind, "own": own,
                    "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, TTL
    except urllib.error.HTTPError as e:
        res, ttl = {"commit": commit, "error": "not on GitHub" if e.code == 404 else f"GitHub said {e.code}"}, FAIL_TTL
    except Exception:  # noqa: BLE001 - offline, timeout, odd JSON: say so, never a bare 500
        res, ttl = {"commit": commit, "error": "couldn't reach GitHub"}, FAIL_TTL
    if len(_cache) >= 32:
        _cache.clear()
    _cache[commit] = (now() + ttl, res)
    return res
