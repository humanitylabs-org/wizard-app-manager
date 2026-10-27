"""Developer release policy. Git history is merged, never rewritten.

Held upstream changes are explicit revert commits, not omitted ancestry. This lets
an independent fix land even when a feature precedes it. A conflicting revert
fails closed; dependencies cannot be guessed safely. Go reverses those reverts.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

from . import release as R

PR = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)")
CORE = re.compile(r"(^|/)(chats?[^/]*(/|$)|[^/]*(?:outbox|boot|service.worker)[^/]*(/|$)|(?:sw\.js|shell\.js|web-update\.js|app\.js|index\.html|desktop\.js|desktop\.html)$)", re.I)


def developer(home):
    return R._read_json(R.wizard_home(home) / "developer.json").get("enabled") is True


def set_developer(value, home=None):
    if type(value) is not bool:
        raise ValueError("developer_mode must be a boolean")
    R._write_json(R.wizard_home(home) / "developer.json", {"enabled": value})
    return {"developer_mode": value}


def require(c):
    if not developer(c.home):
        raise R.GitError("turn on Developer mode in Settings first")
    if c.cfg.get("app", R.DEFAULT_APP) != R.DEFAULT_APP:
        raise R.GitError("My release builds wizard-ux only; WAM itself always uses main")
    url = c.git("remote", "get-url", "origin")
    if re.search(r"wizard-app-manager(?:\.git)?/?$", url):
        raise R.GitError("WAM itself must never be built through My release")
    if c.git("ls-files", "wam/cli.py"):
        raise R.GitError("WAM itself must never be built through My release")


def gh(c, endpoint):
    if c.pr_lookup:
        return c.pr_lookup(endpoint)
    p = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, timeout=45)
    if p.returncode:
        raise R.GitError("could not verify GitHub PR state; no release was changed")
    return json.loads(p.stdout)


def repository(c):
    url = c.git("remote", "get-url", c.cfg.get("remote") or "origin")
    m = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?", url)
    if m:
        return m[1]
    if c.pr_lookup:
        return "fixture/wizard-ux"
    raise R.GitError("upstream must identify a GitHub repository")


def owner_confirm(message):
    # A CLI switch, agent commit trailer or environment variable is not owner consent.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise R.GitError("this choice requires the owner at an interactive terminal")
    if input(message + '\nType KEEP MINE to confirm: ') != "KEEP MINE":
        raise R.GitError("owner did not confirm")


def items(c, refresh=True, replacing=None):
    data = R._read_json(c.p.dir / "items.json").get("items", [])
    result = []
    for item in data:
        if replacing and item.get("pr") == replacing:
            continue
        if item.get("pr") and refresh:
            m = PR.fullmatch(item["pr"])
            doc = gh(c, f"repos/{m[1]}/pulls/{m[2]}")
            if doc.get("merged_at") or doc.get("merged") is True:
                continue  # squash-safe: PR state, never patch-id or commit ancestry
            if item["status"] == "proposed" and doc.get("state") != "open":
                raise R.GitError(f"proposed item {item['pr']} has no open PR")
            if item["status"] == "proposed" and doc.get("head", {}).get("sha") != item["sha"]:
                raise R.GitError(f"proposal head changed; register {item['pr']} again")
        result.append(item)
    if refresh:
        R._write_json(c.p.dir / "items-state.json", {"items": result, "checked_at": R._now()})
    return result


def register(c, status, pr=None, sha=None, title=None):
    require(c)
    with R._locked(c.p) as got:
        if not got:
            raise R.GitError("a release run is already going")
        current = items(c, replacing=pr)
        if status not in ("proposed", "mine"):
            raise R.GitError("an item must be proposed or mine")
        if pr:
            m = PR.fullmatch(pr)
            if not m or m[1] != repository(c):
                raise R.GitError("use an exact PR URL in the source repository")
            doc = gh(c, f"repos/{m[1]}/pulls/{m[2]}")
            if doc.get("state") != "open" or doc.get("merged_at"):
                raise R.GitError("register an open PR")
            pr_sha = doc.get("head", {}).get("sha")
            if sha and sha != pr_sha:
                raise R.GitError("proposed commit must be the linked PR head")
            sha = pr_sha
            title = title or doc.get("title")
        if status == "proposed" and not pr:
            raise R.GitError("agent changes must be proposed with an open PR link")
        if not sha or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise R.GitError("item requires the full commit SHA")
        if not c.ok("merge-base", "--is-ancestor", sha, "HEAD"):
            raise R.GitError("item commit is not in the adopted branch")
        current = [x for x in current if x["sha"] != sha and (not pr or x.get("pr") != pr)]
        if status == "mine":
            if sum(x["status"] == "mine" for x in current) >= 5:
                raise R.GitError("at most five mine items are allowed")
            owner_confirm("Only the owner can keep this change mine.")
        item = {"sha": sha, "title": title or c.git("show", "-s", "--format=%s", sha),
                "status": status, "date": R._now()[:10], "pr": pr}
        registered = R._read_json(c.p.dir / "items.json").get("items", [])
        history = R._read_json(c.p.dir / "items-history.json").get("items", [])
        by_sha = {x["sha"]: x for x in [*history, *registered, item]}
        R._write_json(c.p.dir / "items-history.json", {"items": list(by_sha.values())})
        R._write_json(c.p.dir / "items.json", {"items": [*current, item]})
        R._write_json(c.p.dir / "items-state.json", {"items": [*current, item], "checked_at": R._now()})
        return item


def validate_items(c, base, head):
    active = items(c)
    # Merged PRs disappear from display, but their original heads still account for
    # old squash-merged owner history until upstream is next merged underneath it.
    known = [*R._read_json(c.p.dir / "items-history.json").get("items", []),
             *R._read_json(c.p.dir / "items.json").get("items", [])]
    for sha in c.git("rev-list", "--no-merges", f"{base}..{head}").splitlines():
        generated = R._read_json(c.p.dir / "policy-commits.json").get("commits", [])
        if sha in generated:
            continue
        if not any(c.ok("merge-base", "--is-ancestor", sha, x["sha"]) for x in known):
            raise R.GitError(f"unregistered owner commit {sha[:8]}; register its open PR before building")
    if sum(x["status"] == "mine" for x in active) > 5:
        raise R.GitError("at most five mine items are allowed")
    return active


def pushed(c, head):
    c.git("fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*", timeout=300)
    refs = c.git("for-each-ref", f"--contains={head}", "--format=%(refname)", "refs/remotes/origin/")
    if not refs:
        raise R.GitError("HEAD is not pushed to origin; push before building")


def publish(c, head):
    # Automation may publish its generated merge/revert commits, never unpublished
    # agent input (the starting HEAD was already verified by pushed()).
    c.git("push", "-q", "origin", f"{head}:refs/heads/release-candidates/{head}", timeout=120)
    pushed(c, head)


def pending(c, target):
    head = c.git("rev-parse", "HEAD")
    out = []
    for sha in c.git("rev-list", "--first-parent", "--reverse", f"{head}..{target}").splitlines():
        prs = gh(c, f"repos/{repository(c)}/commits/{sha}/pulls")
        doc = next((p for p in prs if p.get("merged_at") and p.get("merge_commit_sha") == sha), None)
        labels = {x["name"] for x in (doc or {}).get("labels", [])}
        out.append({"sha": sha, "title": (doc or {}).get("title") or c.git("show", "-s", "--format=%s", sha),
                    "pr": (doc or {}).get("html_url"), "kind": "fix" if "fix" in labels and "feature" not in labels else "feature",
                    "merged_at": (doc or {}).get("merged_at"), "deadline_hours": 72})
    return out


def revert(c, w, sha, message):
    parents = c.git("show", "-s", "--format=%P", sha, cwd=w).split()
    c.git("revert", "--no-commit", *(["-m", "1"] if len(parents) > 1 else []), sha, cwd=w)
    c.git("commit", "-q", "--allow-empty", "-m", "WAM policy: " + message, cwd=w)
    head = c.git("rev-parse", "HEAD", cwd=w)
    generated = R._read_json(c.p.dir / "policy-commits.json").get("commits", [])
    R._write_json(c.p.dir / "policy-commits.json", {"commits": [*generated, head]})
    return head


class Conflict(R.GitError):
    def __init__(self, message, notified):
        super().__init__(message)
        self.notified = notified


def candidate(c, target, new, held, approve=()):
    ours = c.git("rev-parse", "HEAD")
    merged, clashed = R.merge_upstream(c, target)
    if not merged:
        files = c.git("diff", "--name-only", f"HEAD...{target}").splitlines()
        notified = R.notify_clash(c, target[:8], files)
        R.check(c)
        raise Conflict(f"CONFLICT a change of yours clashes with {c.label} {target[:8]}; kept the running build", notified)
    records = list(held)
    with R._worktree(c, merged, "wam-release-policy.") as w:
        for item in list(records):
            if item["sha"] in approve:
                revert(c, w, item["revert"], "owner go " + item["sha"])
                records.remove(item)
        for item in reversed(new):
            if item["kind"] != "fix" and item["sha"] not in approve:
                rev = revert(c, w, item["sha"], "hold " + item["sha"])
                records.insert(0, {**item, "revert": rev})
        head = c.git("rev-parse", "HEAD", cwd=w)
        paths = sorted(set(c.git("diff", "--name-only", ours, merged, cwd=w).splitlines()) | set(c.git("diff", "--name-only", f"{ours}...{target}", cwd=w).splitlines()))
        resolver = {"sha": merged, "resolver": True, "ours": ours, "theirs": target, "files": paths,
                    "core": any(CORE.search(x) for x in paths)} if clashed else None
        return head, records, resolver


def execute(c, action, approve=()):
    require(c)
    if c.git("status", "--porcelain"):
        raise R.GitError("uncommitted edits or untracked files; kept the running build")
    if c.git("branch", "--show-current") != c.branch:
        raise R.GitError("checkout is not on the configured release branch")
    initial = c.git("rev-parse", "HEAD")
    pushed(c, initial)
    target = R.fetch_upstream(c)
    base = c.git("merge-base", initial, target)
    validate_items(c, base, initial)
    held = R._read_json(c.p.dir / "holds.json").get("items", [])
    new = pending(c, target) if action != "rebuild" else []
    if action == "rebuild":
        return R.build(c, "rebuild", policy_checked=True)
    all_features = [x for x in [*held, *new] if x.get("kind") != "fix"]
    selected = set(approve)
    allowed = {x["sha"] for x in all_features}
    if not selected <= allowed:
        raise R.GitError("go names an item outside the current batch")
    stage = R._read_json(c.p.dir / "staging.json")
    if action == "go":
        if not stage or stage.get("target") != target or stage.get("owner_head") != initial:
            raise R.GitError("staging changed; run update and review the new batch before go")
        if not approve:
            selected = allowed
    if action != "go" and (stage.get("resolver") or {}).get("core") and stage.get("target") == target and stage.get("owner_head") == initial:
        return stage
    # One staging image for the entire feature batch, without touching live.
    fingerprint = {"target": target, "owner_head": initial, "items": sorted(allowed)}
    if all_features and any(stage.get(k) != v for k, v in fingerprint.items()):
        full, _, resolver = candidate(c, target, new, held, allowed)
        if resolver:
            history = R._read_json(c.p.dir / "resolvers.json").get("merges", [])
            R._write_json(c.p.dir / "resolvers.json", {"merges": [*history, resolver]})
        publish(c, full)
        staged = R.build(c, "rebuild", policy_checked=True, head_override=full, stage_only=True)
        if staged["state"] != "staged":
            return staged
        stage = {**fingerprint, **staged, "resolver": resolver}
        R._write_json(c.p.dir / "staging.json", stage)
    if action == "go" and (stage.get("resolver") or {}).get("core"):
        if selected != allowed:
            raise R.GitError("core resolver batch requires a new staging build for partial go")
        head = stage["source_revision"]
        result = R.build(c, "rebuild", policy_checked=True, head_override=head)
        if result["state"] == "ok":
            c.git("merge", "--ff-only", head)
            c.git("push", "-q", "origin", c.branch, timeout=120)
            (c.p.dir / "staging.json").unlink(missing_ok=True)
        return result
    head, records, resolver = candidate(c, target, new, held, selected)
    if resolver:
        history = R._read_json(c.p.dir / "resolvers.json").get("merges", [])
        R._write_json(c.p.dir / "resolvers.json", {"merges": [*history, resolver]})
        if resolver["core"] and action != "go":
            publish(c, head)
            staged = R.build(c, "rebuild", policy_checked=True, head_override=head, stage_only=True)
            R._write_json(c.p.dir / "staging.json", {**fingerprint, **staged, "resolver": resolver})
            return staged
    if head == initial:
        return {"state": "waiting" if held else "checked", "staging": stage}
    publish(c, head)
    result = R.build(c, "rebuild", policy_checked=True, head_override=head)
    if result["state"] == "ok":
        c.git("merge", "--ff-only", head)
        c.git("push", "-q", "origin", c.branch, timeout=120)
        R._write_json(c.p.dir / "holds.json", {"items": records})
        if records:
            # The reviewed full candidate is still valid after policy-only reverts.
            R._write_json(c.p.dir / "staging.json", {**stage, "owner_head": head, "items": sorted(x['sha'] for x in records)})
        else:
            (c.p.dir / "staging.json").unlink(missing_ok=True)
        R.check(c)
    return {**result, "held": records, "staging": stage}
