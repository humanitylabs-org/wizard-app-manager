"""The shared Wizard App Index (https://apps.aiwizards.com): search, and resolve an app id to a pinned repo commit.

The index is only discovery. Everything it returns is re-checked locally before install.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

DEFAULT_INDEX = "https://apps.aiwizards.com"


class IndexError_(RuntimeError):
    pass


def base(cfg: dict | None = None) -> str:
    return (os.environ.get("WAM_INDEX") or (cfg or {}).get("index") or DEFAULT_INDEX).rstrip("/")


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "wizard-app-manager", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise IndexError_(f"index {url} -> {e.code}: {e.read()[:300]!r}") from None
    except OSError as e:
        raise IndexError_(f"index unreachable ({url}): {e}") from None


def search(index: str, q: str = "", **filters) -> list[dict]:
    params = {"q": q, "limit": "50", **{k: v for k, v in filters.items() if v}}
    return _get(f"{index}/v1/search?{urllib.parse.urlencode(params)}").get("apps", [])


def resolve(index: str, ref: str) -> dict:
    """`ref` is an app id (e.g. transcriber) or a repo (owner/name). Returns one index record."""
    if "/" in ref:
        owner, repo = ref.strip("/").removeprefix("https://github.com/").split("/")[:2]
        rec = _get(f"{index}/v1/apps/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}")
        if not rec.get("listed", True) or not rec.get("install"):
            fails = (rec.get("last_check") or {}).get("failures") or ["not listed"]
            raise IndexError_(f"{ref} is not listed in the index: {', '.join(fails)}")
        return rec
    hits = search(index, id=ref)
    if not hits:
        raise IndexError_(f"no app with id '{ref}' in the index; try `wam search {ref}`")
    if len(hits) > 1:
        others = ", ".join(h["repo"] for h in hits[1:])
        print(f"note: '{ref}' is published by several repos; using {hits[0]['repo']} (others: {others}). "
              f"Install a specific one with `wam install owner/repo`.")
    return hits[0]


def all_listed(index: str, page: int = 50, cap: int = 2000) -> list[dict]:
    """Every listed app, paged. Fine while the index is small; at large scale a box mirrors a chosen subset
    (defaults, featured, installed) instead of everything, and search stays in the index."""
    out, offset = [], 0
    while offset is not None and len(out) < cap:
        d = _get(f"{index}/v1/search?{urllib.parse.urlencode({'q': '', 'limit': str(page), 'offset': str(offset)})}")
        out += d.get("apps", [])
        offset = d.get("next_offset")
    return out
