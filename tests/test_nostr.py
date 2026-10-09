"""Nostr catalog source: signature checks, NIP-34 parsing, publisher rule, discover() wiring. No network."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import nostr, store, wizardmd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "hello-wizard"


# --- a tiny BIP-340 signer, test-only (the module itself only verifies) ---

def _sign(seckey: int, msg: bytes) -> tuple[str, bytes]:
    P = nostr._mul(nostr._G, seckey)
    d = seckey if P[1] % 2 == 0 else nostr._N - seckey
    px = P[0].to_bytes(32, "big")
    t = (d ^ int.from_bytes(nostr._tagged("BIP0340/aux", bytes(32)), "big")).to_bytes(32, "big")
    k0 = int.from_bytes(nostr._tagged("BIP0340/nonce", t + px + msg), "big") % nostr._N
    R = nostr._mul(nostr._G, k0)
    k = k0 if R[1] % 2 == 0 else nostr._N - k0
    rx = R[0].to_bytes(32, "big")
    e = int.from_bytes(nostr._tagged("BIP0340/challenge", rx + px + msg), "big") % nostr._N
    return px.hex(), rx + ((k + e * d) % nostr._N).to_bytes(32, "big")


def _event(seckey: int, tags: list, created_at: int = 1_790_000_000, kind: int = 30617) -> dict:
    pub, _ = _sign(seckey, b"\0" * 32)
    ev = {"pubkey": pub, "created_at": created_at, "kind": kind, "tags": tags, "content": ""}
    ev["id"] = nostr.event_id(ev)
    _, sig = _sign(seckey, bytes.fromhex(ev["id"]))
    ev["sig"] = sig.hex()
    return ev


def _ann(seckey: int, d: str, clone: str, tags=("wizard-os-app",), **kw) -> dict:
    t = [["d", d], ["name", d], ["clone", clone], ["relays", "wss://relay.example"],
         ["r", "a" * 40, "euc"]] + [["t", x] for x in tags]
    return _event(seckey, t, **kw)


# --- primitives ---

def test_bip340_official_vectors():
    # BIP-340 test vectors 0 and 1 (verify path)
    assert nostr.schnorr_verify(
        bytes(32),
        bytes.fromhex("F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9"),
        bytes.fromhex("E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA821525F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0"))
    assert nostr.schnorr_verify(
        bytes.fromhex("243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89"),
        bytes.fromhex("DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659"),
        bytes.fromhex("6896BD60EEAE296DB48A229FF71DFE071BDE413E6D43F917DC8DCF8C78DE33418906D11AC976ABCCB20B091292BFF4EA897EFCB639EA871CFA95F6DE339E4B0A"))


def test_signed_event_verifies_and_tamper_fails():
    ev = _ann(7, "hello-wizard", "https://example.com/a.git")
    assert nostr.verify_event(ev)
    bad = json.loads(json.dumps(ev))
    bad["tags"].append(["t", "extra"])
    assert not nostr.verify_event(bad)           # id no longer matches content
    bad["id"] = nostr.event_id(bad)
    assert not nostr.verify_event(bad)           # id matches, signature does not


def test_npub_roundtrip():
    pub = _sign(7, b"\0" * 32)[0]
    n = nostr.hex_to_npub(pub)
    assert n.startswith("npub1") and nostr.npub_to_hex(n) == pub
    # James's published npub decodes
    assert len(nostr.npub_to_hex("npub168zsf6n5v8kaksatc74m22zp3xwle2k7s25ukp6y35cr6vjcp7yq740m54")) == 64


# --- NIP-34 discovery ---

def test_announcements_verify_dedupe_and_newest_wins():
    old = _ann(7, "app", "https://example.com/old.git", created_at=100)
    new = _ann(7, "app", "https://example.com/new.git", created_at=200)
    other = _ann(9, "app", "https://example.com/other.git")
    untagged = _ann(11, "nope", "https://example.com/x.git", tags=("something-else",))
    forged = dict(_ann(13, "evil", "https://example.com/evil.git"), sig="00" * 64)
    relays = {"wss://r1": [old, other, forged], "wss://r2": [new, untagged]}
    anns, errors = nostr.announcements("wizard-os-app", list(relays), fetch=lambda r, f: relays[r])
    got = {(a["d"], a["clone"]) for a in anns}
    assert got == {("app", "https://example.com/new.git"), ("app", "https://example.com/other.git")}
    assert any("bad signature" in e for e in errors)
    assert anns[0]["euc"] == "a" * 40


def test_relay_failure_is_an_error_not_a_crash():
    def boom(relay, filt):
        raise ConnectionError("down")
    anns, errors = nostr.announcements("wizard-os-app", ["wss://down"], fetch=boom)
    assert anns == [] and errors == ["nostr wss://down: down"]


def test_authors_filter_accepts_npub():
    seen = {}
    pub = _sign(7, b"\0" * 32)[0]
    nostr.announcements("wizard-os-app", ["wss://r"], authors=[nostr.hex_to_npub(pub)],
                        fetch=lambda r, f: seen.update(f) or [])
    assert seen["authors"] == [pub] and seen["#t"] == ["wizard-os-app"] and seen["kinds"] == [30617]


def test_publisher_rule():
    ann = nostr.parse_announcement(_ann(7, "app", "https://x"))
    assert nostr.publisher_rule(None, ann)[1]
    assert nostr.publisher_rule(ann["npub"], ann)[1]
    stranger = nostr.hex_to_npub(_sign(9, b"\0" * 32)[0])
    assert not nostr.publisher_rule(stranger, ann)[1]
    ann["maintainers"].append(nostr.npub_to_hex(stranger))
    assert nostr.publisher_rule(stranger, ann)[1]


def test_checker_rejects_bad_publisher(tmp_path):
    app = tmp_path / "app"
    shutil.copytree(TEMPLATE, app)
    f = app / "wizard.md"
    f.write_text(f.read_text().replace("wizard: 0.2\n", "wizard: 0.2\npublisher: not-an-npub\n", 1))
    assert "publisher is an npub" in wizardmd.failures(wizardmd.check(app))


def _git_app(tmp_path: Path, publisher: str | None) -> Path:
    app = tmp_path / "remote-app"
    shutil.copytree(TEMPLATE, app)
    if publisher:
        f = app / "wizard.md"
        f.write_text(f.read_text().replace("wizard: 0.2\n", f"wizard: 0.2\npublisher: {publisher}\n", 1))
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=app, check=True)
    return app


def test_discover_nostr_source_end_to_end(tmp_path):
    signer = nostr.hex_to_npub(_sign(7, b"\0" * 32)[0])
    app = _git_app(tmp_path, signer)
    ann = _ann(7, "hello-wizard", f"file://{app}")
    cands, errors = store.discover([{"nostr": "wizard-os-app", "relays": ["wss://r"]}], tmp_path / "cache",
                                   nostr_fetch=lambda r, f: [ann])
    assert not errors and len(cands) == 1 and cands[0].ok, (errors, [c.results for c in cands])
    assert cands[0].origin == f"nostr:{signer}/hello-wizard"
    cat = store.compile_store(cands, tmp_path / "store")
    m = cat["apps"][0]
    assert m["found_via"] == "nostr" and m["publisher"] == signer and len(m["commit"]) == 40
    assert m["nostr"]["event"] == ann["id"]


def test_discover_nostr_wrong_signer_is_skipped(tmp_path):
    publisher = nostr.hex_to_npub(_sign(9, b"\0" * 32)[0])      # wizard.md says key 9
    app = _git_app(tmp_path, publisher)
    ann = _ann(7, "hello-wizard", f"file://{app}")             # but key 7 announced it
    cands, _ = store.discover([{"nostr": "wizard-os-app", "relays": ["wss://r"]}], tmp_path / "cache",
                              nostr_fetch=lambda r, f: [ann])
    assert not cands[0].ok
    cat = store.compile_store(cands, tmp_path / "store")
    assert cat["apps"] == [] and "nostr signer matches wizard.md publisher" in cat["skipped"][0]["failed"]


def test_same_repo_on_github_and_nostr_listed_once(tmp_path, monkeypatch):
    app = _git_app(tmp_path, None)
    url = f"file://{app}"
    monkeypatch.setattr(store, "topic_repos", lambda topic, token=None: [url])
    ann = _ann(7, "hello-wizard", url + ".git")
    cands, _ = store.discover([{"topic": "wizard-os-app"}, {"nostr": "wizard-os-app", "relays": ["wss://r"]}],
                              tmp_path / "cache", nostr_fetch=lambda r, f: [ann])
    assert len(cands) == 1 and cands[0].nostr is None


def test_nostr_is_off_by_default(monkeypatch):
    monkeypatch.delenv("WAM_NOSTR", raising=False)
    assert not nostr.enabled({})
    assert nostr.enabled({"nostr": True})
    monkeypatch.setenv("WAM_NOSTR", "1")
    assert nostr.enabled({})
