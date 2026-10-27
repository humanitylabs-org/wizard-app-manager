#!/usr/bin/env python3
"""Isolated real-git / real-Docker acceptance. Run on the Linux worker only.

Usage: python tools/qa_release.py --ux /path/to/wizard-ux --out evidence
GitHub PR/label responses and the interactive owner confirmation are explicit
fixtures. Builds, HTTP reads, origin pushes and the running container are real.
No existing Wizard, systemd unit, registry or public repository is modified.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import release as R, release_policy as P


def cmd(argv, **kw):
    return subprocess.run([str(x) for x in argv], check=True, capture_output=True, text=True, **kw).stdout.strip()


def git(root, *argv):
    return cmd(["git", "-C", root, "-c", "user.name=Release QA", "-c", "user.email=qa@example.invalid", *argv])


def commit(root, path, text, title):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(root, "add", "-A"); git(root, "commit", "-q", "-m", title)
    return git(root, "rev-parse", "HEAD")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ux", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    # Exercise the same private-service environment as the durable worker. Docker
    # must not inherit owner-only source modes into its unprivileged runtime.
    os.umask(0o077)
    a.out.mkdir(parents=True, exist_ok=True)
    records = []
    name = "wam-release-qa-" + str(os.getpid())
    live: dict[str, str | None] = {"url": None}
    images = set()
    def record(test, **data):
        records.append({"test": test, "pass": True, **data})
        (a.out / "release-e2e.json").write_text(json.dumps(records, indent=2) + "\n")
    try:
        with tempfile.TemporaryDirectory(prefix="wam-release-e2e.") as tmp:
            root = Path(tmp)
            up = root / "upstream"; bare = root / "origin.git"; home = root / "box"
            cmd(["git", "clone", "-q", "--no-hardlinks", a.ux, up]); git(up, "checkout", "-q", "-B", "main")
            # Conflict fixtures are ordinary owner files, not changes to the test harness.
            commit(up, "web/release-test-mine.txt", "base\n", "Fixture base")
            cmd(["git", "clone", "-q", "--bare", up, bare])
            git(up, "remote", "set-url", "origin", str(bare))
            home.mkdir(); P.set_developer(True, home)
            rt = root / "runtipi"; app = rt / "apps/wizardapps/wizard-ux"; app.mkdir(parents=True)
            (app / "docker-compose.generated.yml").write_text("services: {}\n")
            ov = rt / "user-config/wizardapps/wizard-ux/docker-compose.yml"; ov.parent.mkdir(parents=True)
            ov.write_text("services:\n  wizard-ux:\n    image: wizard-ux:fixture-before\n")
            R._write_json(home / "app-manager.json", {"runtipi_root": str(rt)})
            R._write_json(home / "apps.json", {"apps": [{"id": "wizard-ux", "provides": ["shell"], "status": "running", "health": "http://127.0.0.1:18796/shell-health", "view": {"local": "http://127.0.0.1:18796/wizard-os-home/"}}]})
            R.init(home, name="QA release", channel="main", source=str(bare), timer=False, systemctl=False,
                   resolver="none", test_cmd="node --test tests/my-release.test.mjs")
            src = R.ctx(home).src
            recreations = 0
            def capture_container(label):
                for action in ("inspect", "logs"):
                    result = subprocess.run(["docker", action, name], capture_output=True, text=True)
                    (a.out / f"container-{label}-{action}.log").write_text(result.stdout + result.stderr)
            def runner(argv, **kw):
                nonlocal recreations
                if argv[:2] == ["docker", "compose"]:
                    recreations += 1
                    capture_container(f"{recreations:02d}-before")
                    image = next(x.split("image:", 1)[1].strip() for x in ov.read_text().splitlines() if "image:" in x)
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
                    # Retain a failed process until its logs have been captured.
                    result = subprocess.run(["docker", "run", "-d", "--name", name,
                        "-p", "127.0.0.1::8788", "-e", "WIZARD_AGENT_SERVICE_ORIGIN=http://127.0.0.1:9", image], capture_output=True, text=True)
                    live["url"] = None
                    capture_container(f"{recreations:02d}-started")
                    if result.returncode == 0:
                        port = subprocess.run(["docker", "port", name, "8788/tcp"], capture_output=True, text=True)
                        if port.returncode or not port.stdout.strip():
                            return subprocess.CompletedProcess(argv, 1, result.stdout, port.stderr or "container has no listening port")
                        live["url"] = "http://" + port.stdout.strip()
                    return result
                if argv[:2] == ["docker", "build"]:
                    images.add(argv[argv.index("-t") + 1])
                return subprocess.run(argv, **kw)
            def probe(url, timeout=3):
                if not live["url"]:
                    return None
                return R._probe(url.replace("http://127.0.0.1:18796", live["url"]), timeout)
            docs = {}
            def lookup(endpoint):
                if "/commits/" in endpoint:
                    return docs.get(endpoint.split("/commits/")[1].split("/")[0], [])
                return docs[endpoint.rsplit("/", 1)[-1]]
            def run(action, **kw):
                got = R.run(action, home, run=runner, probe=probe, pr_lookup=lookup, wait=30, **kw)
                (a.out / (action + "-last.json")).write_text(json.dumps(got, indent=2) + "\n")
                return got
            def get(path):
                assert live["url"], "the fixture has no running shell"
                with urlopen(live["url"] + "/wizard-os-home/" + path, timeout=5) as r:
                    return r.read().decode()
            proposed = commit(src, "web/release-test-proposed.txt", "proposed\n", "Proposed owner change")
            git(src, "push", "-q", "origin", "HEAD")
            docs["1"] = {"state": "open", "title": "Proposed owner change", "head": {"sha": proposed}}
            c = R.ctx(home, pr_lookup=lookup)
            P.register(c, "proposed", "https://github.com/fixture/wizard-ux/pull/1")
            mine = commit(src, "web/release-test-mine.txt", "owner\n", "Owner mine")
            git(src, "push", "-q", "origin", "HEAD")
            with patch.object(P, "owner_confirm"):  # simulated owner's explicit choice
                P.register(c, "mine", sha=mine)
            assert run("rebuild")["state"] == "ok"
            assert get("release-test-proposed.txt") == "proposed\n" and get("release-test-mine.txt") == "owner\n"
            record("init main; one proposed plus one owner-confirmed mine", head=mine)
            feature = commit(up, "web/release-test-feature.txt", "feature\n", "Merged feature")
            fix = commit(up, "web/release-test-fix.txt", "fixed\n", "Merged fix after feature")
            git(up, "push", "-q", "origin", "main")
            for sha, kind in ((feature, "feature"), (fix, "fix")):
                docs[sha] = [{"merged_at": "2026-10-27T00:00:00Z", "merge_commit_sha": sha, "title": kind, "labels": [{"name": kind}]}]
            got = run("auto"); assert got["state"] == "ok", got
            assert get("release-test-fix.txt") == "fixed\n"
            assert not (src / "web/release-test-feature.txt").exists()
            assert [x["sha"] for x in got["held"]] == [feature]
            stage = R._read_json(c.p.dir / "staging.json")
            staged_feature = cmd(["docker", "run", "--rm", "--entrypoint", "cat", stage["image"], "/usr/share/nginx/wizard-ux/release-test-feature.txt"])
            assert staged_feature == "feature"
            full = git(src, "rev-parse", "HEAD")
            built = R._read_json(c.p.built)
            label = cmd(["docker", "image", "inspect", built["image"], "--format", '{{index .Config.Labels "org.opencontainers.image.revision"}}'])
            container_label = cmd(["docker", "inspect", name, "--format", '{{index .Config.Labels "org.opencontainers.image.revision"}}'])
            assert label == container_label == full == built["source_revision"]
            record("fix auto applies after feature; feature waits in one staging build", fix=fix, feature=feature, staging=stage["source_revision"], live=full, image_label=label, container_label=container_label)
            assert run("auto")["state"] == "waiting"
            record("same upstream batch is not rebuilt every timer tick")
            assert run("go")["state"] == "ok"
            assert get("release-test-feature.txt") == "feature\n"
            record("owner go promotes feature")
            before = R._read_json(c.p.built)
            cid = cmd(["docker", "inspect", name, "--format", "{{.Id}}"])
            clash = commit(up, "web/release-test-mine.txt", "upstream\n", "Conflicting merged fix")
            git(up, "push", "-q", "origin", "main")
            docs[clash] = [{"merged_at": "2026-10-27T00:00:00Z", "merge_commit_sha": clash, "labels": [{"name": "fix"}]}]
            assert run("auto")["state"] == "conflict"
            assert R._read_json(c.p.built) == before
            assert cmd(["docker", "inspect", name, "--format", "{{.Id}}"]).strip() == cid
            assert get("release-test-mine.txt") == "owner\n"
            record("conflicting upstream fix keeps exact last working build", build=before["source_revision"], container=cid)
            # A real Docker build that starts but never listens must roll back to
            # the previous image, not merely restore a YAML file or fake a probe.
            old_override = ov.read_text()
            broken = commit(src, "packaging/entrypoint.sh",
                "#!/bin/sh\nprintf 'intentional unhealthy release fixture\\n' >&2\nexec sleep 120\n",
                "Fixture: deliberately unhealthy shell")
            git(src, "push", "-q", "origin", "HEAD")
            with patch.object(P, "owner_confirm"):
                P.register(c, "mine", sha=broken)
            got = run("rebuild")
            assert got["state"] == "rolled_back", got
            assert ov.read_text() == old_override
            assert R._read_json(c.p.built) == before
            assert R.serving(R.ctx(home, probe=probe), before["build"]), "rollback image did not become healthy"
            assert json.loads(get("my-release.json"))["source_revision"] == before["source_revision"]
            assert cmd(["docker", "inspect", name, "--format", '{{index .Config.Labels "org.opencontainers.image.revision"}}']) == before["source_revision"]
            assert get("release-test-mine.txt") == "owner\n"
            assert any("intentional unhealthy release fixture" in p.read_text() for p in a.out.glob("container-*-before-logs.log"))
            record("deliberately unhealthy real build rolls back and serves exact last working build",
                   broken=broken, restored=before["source_revision"])
            (a.out / "result.json").write_text(json.dumps({"pass": True, "assertions": len(records)}, indent=2) + "\n")
            print(json.dumps({"pass": True, "evidence": str(a.out / "release-e2e.json")}))
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        for image in images:
            subprocess.run(["docker", "image", "rm", image], capture_output=True)


if __name__ == "__main__":
    main()
