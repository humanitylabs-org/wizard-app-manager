"""Review findings on the owner-proof key handling (PR 420 review, bounded source review at 770d8b31)."""
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import owner_edge, owner_proof as op  # noqa: E402


def test_a_creator_that_lost_the_race_never_replaces_the_winners_key(tmp_path, monkeypatch):
    """P1: both callers pass exists(), then both publish; the second must not overwrite the first."""
    home = tmp_path / "h"
    winner = bytes([7]) * 32
    real = op.secrets.token_bytes

    def losing_draw(n):
        op.edge_key_path(home, "messenger").write_bytes(winner)      # the other caller publishes first
        os.chmod(op.edge_key_path(home, "messenger"), 0o600)
        return real(n)
    monkeypatch.setattr(op.secrets, "token_bytes", losing_draw)
    assert op.ensure_edge_key(home, "messenger") is False
    assert op.edge_key_path(home, "messenger").read_bytes() == winner
    assert not [p for p in op.key_dir(home, "messenger").iterdir() if p.name.startswith(".")]   # no temp left behind


def test_an_adopter_that_lost_the_race_to_a_different_key_is_refused(tmp_path, monkeypatch):
    """P1: the adoption mismatch check also covers a key that appeared after the exists() check."""
    home = tmp_path / "h"
    op._private_dir(home / "owner-proof")
    old = tmp_path / "old.key"
    old.write_bytes(bytes([1]) * 32)
    old.chmod(0o600)
    winner = bytes([2]) * 32
    path = op.edge_key_path(home, "messenger")
    real_exists, calls = Path.exists, []

    def stale_exists(self):
        if self == path and not calls:        # the check that raced: the key was not there yet
            calls.append(1)
            op._private_dir(path.parent)
            path.write_bytes(winner)
            path.chmod(0o600)
            return False
        return real_exists(self)
    monkeypatch.setattr(Path, "exists", stale_exists)
    with pytest.raises(ValueError, match="different owner proof key"):
        op.ensure_edge_key(home, "messenger", adopt=old)
    assert path.read_bytes() == winner


def test_many_concurrent_first_provisions_publish_exactly_one_key(tmp_path, monkeypatch):
    home, n = tmp_path / "h", 24
    drawn, local = {}, threading.local()
    counter = iter(range(1, 1000))
    lock = threading.Lock()

    def draw(size):
        with lock:
            key = bytes([next(counter)]) * size
            if size == op.KEY_BYTES:       # (secrets.token_hex also draws, for temporary file names)
                local.key = key
        return key
    monkeypatch.setattr(op.secrets, "token_bytes", draw)
    barrier, results = threading.Barrier(n), []

    def go():
        barrier.wait()
        local.key = None
        won = op.ensure_edge_key(home, "messenger")
        results.append((won, local.key))
    ts = [threading.Thread(target=go) for _ in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    winners = [k for won, k in results if won]
    assert len(results) == n and len(winners) == 1
    assert op.edge_key_path(home, "messenger").read_bytes() == winners[0]


def test_the_cached_key_stops_signing_when_its_permissions_loosen(tmp_path):
    """P2: warm the cache, loosen the mode (inode, mtime and size do not change): the next use is refused."""
    home = tmp_path / "h"
    op.ensure_edge_key(home, "messenger")
    cache = owner_edge._KeyCache(op.edge_key_path(home, "messenger"))
    assert len(cache()) == 32
    os.chmod(op.edge_key_path(home, "messenger"), 0o644)
    with pytest.raises(ValueError):
        cache()
    os.chmod(op.edge_key_path(home, "messenger"), 0o600)
    assert len(cache()) == 32                                   # and recovers once it is private again


def test_the_cached_key_follows_a_rotation(tmp_path):
    home = tmp_path / "h"
    op.ensure_edge_key(home, "messenger")
    path = op.edge_key_path(home, "messenger")
    cache = owner_edge._KeyCache(path)
    first = cache()
    path.write_bytes(bytes([9]) * 32)
    assert cache() == bytes([9]) * 32 != first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
