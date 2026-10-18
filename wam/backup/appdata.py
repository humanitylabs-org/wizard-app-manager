"""One app's data, archived and put back as root by a one-off container of the App Manager's own image.

Why not Runtipi's per-app backup (POST /backups/<urn>/backup and /restore)? On a real box it loses data silently:
it copies the app folders with Node's fs.cp, which aborts part way on the first socket (an agent's gateway.sock under
the Agent Service's data) and the error is only logged, so the archive is quietly incomplete; it skips every path
containing "backups"; it records folders as root-owned; it turns relative symlinks into absolute ones; and its restore
refuses any archive that holds a symlink ("Backup contains unsupported file types"), so an app with a model cache
(Hugging Face snapshots are symlinks into blobs/) can never be restored from its own backup.

Here the engine stops the app through Runtipi, then runs `tar` (busybox, from the image the Runtipi container
already runs: no download, no network) as root with the app's folders mounted read-only and streams the archive to
the engine, which checks every member and writes the same layout Runtipi uses (`app-data/`, `app/`, `user-config/`)
plus `volumes/<name>/` for the named Docker volumes the app's compose file declares (an AI account login, say). Owners,
modes and mtimes are kept exactly; sockets are skipped and reported (they are recreated by the running app).

Putting back extracts into a staging folder next to the app's data (same filesystem), carries over the parts of the
live folder this box keeps (an agent's home under the Agent Service, the Agent Service's MCP token), then swaps the
folders with renames. Member rules (both directions, and on import): safe relative names under the four roots,
regular files, folders, hard links to earlier files, and symlinks whose target stays inside the root they live in;
nothing below a symlink. Anything else is refused.
"""
from __future__ import annotations

import io
import json
import os
import posixpath
import re
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path

PARTS = ("app-data", "app", "user-config")
ROOTS = PARTS + ("volumes",)
VOLUME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
AGENT_SERVICE_APP = "wizard-agent-service"   # = box.AGENT_SERVICE_APP
# Parts of an app's data that belong to this box and never travel (relative to its app-data folder).
KEEP_IN_PLACE = {AGENT_SERVICE_APP: ("data/wizard-os/agents", "data/wizard-os/mcp-token")}


class AppDataError(RuntimeError):
    pass


class UnsafeMember(AppDataError):
    pass


def _clean(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/")


class MemberGuard:
    """Checks the members of one app archive in order (see the module doc for the rules)."""

    def __init__(self) -> None:
        self.files: set[str] = set()
        self.links: set[str] = set()
        self.entries = 0
        self.bytes = 0

    def check(self, m: tarfile.TarInfo) -> str | None:
        """The clean name, None for the archive root, or UnsafeMember."""
        name = _clean(m.name)
        if name in ("", "."):
            return None
        parts = name.split("/")
        if name.startswith("/") or "\x00" in name or "\\" in name or any(p in ("", ".", "..") for p in parts):
            raise UnsafeMember(f"an app data archive holds an unsafe name: {name[:120]!r}")
        if parts[0] not in ROOTS:
            raise UnsafeMember(f"an app data archive holds an unexpected entry: {name[:120]!r}")
        if parts[0] == "volumes" and (len(parts) < 2 and not m.isdir() or len(parts) >= 2 and not VOLUME_NAME.match(parts[1])):
            raise UnsafeMember(f"an app data archive holds a bad volume name: {name[:120]!r}")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in self.links:
                raise UnsafeMember(f"an app data archive holds an entry below a link: {name[:120]!r}")
        if m.issym():
            if not link_inside(name, m.linkname):
                raise UnsafeMember(f"an app data archive holds a link that leaves its folder: {name[:120]!r}")
            self.links.add(name)
        elif m.islnk():
            if _clean(m.linkname) not in self.files:
                raise UnsafeMember(f"an app data archive holds a hard link to an unknown file: {name[:120]!r}")
        elif m.isreg() and not m.issparse():
            self.files.add(name)
            self.bytes += m.size
        elif not m.isdir():
            raise UnsafeMember(f"an app data archive holds a device, FIFO or special file: {name[:120]!r}")
        self.entries += 1
        return name


def link_root(name: str) -> str:
    parts = name.split("/")
    return "/".join(parts[:2]) if parts[0] == "volumes" else parts[0]


def link_inside(name: str, target: str) -> bool:
    """A relative symlink `name` -> `target` that resolves inside its own root (app-data/, app/, user-config/ or one
    volume)."""
    if not target or target.startswith("/") or "\x00" in target:
        return False
    root = link_root(name)
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    return resolved == root or resolved.startswith(root + "/")


def check_archive(fileobj, mode: str = "r|gz") -> dict:
    """Read a whole app archive and check every member. Returns {entries, bytes}."""
    g = MemberGuard()
    with tarfile.open(fileobj=fileobj, mode=mode) as t:
        for m in t:
            g.check(m)
    return {"entries": g.entries, "bytes": g.bytes}


def compose_volumes(package: Path, project: str) -> list[str]:
    """Named volumes an app's compose file declares: `name:` when given, else Compose's `<project>_<key>`. Volumes
    marked external, or with an interpolated name, are left out."""
    for fn in ("docker-compose.yml", "docker-compose.yaml", "docker-compose.json"):
        p = package / fn
        if p.is_file():
            break
    else:
        return []
    try:
        import yaml
        doc = yaml.safe_load(p.read_text()) or {}
    except Exception:  # noqa: BLE001 - an unreadable compose file declares nothing we can capture
        return []
    out = []
    for key, spec in ((doc.get("volumes") or {}) if isinstance(doc, dict) else {}).items():
        spec = spec if isinstance(spec, dict) else {}
        if spec.get("external"):
            continue
        name = str(spec.get("name") or f"{project}_{key}")
        if "$" in name or not VOLUME_NAME.match(name):
            continue
        out.append(name)
    return sorted(set(out))


RESTORE_SH = r'''
set -eu
S="$RT/app-data/$STORE/.$APP.wizard-new"
trap 'rm -rf "$S"' EXIT
rm -rf "$S"
mkdir -p "$S"
chmod 700 "$S"
tar -xpf - --numeric-owner -C "$S"
if [ -f "$S/.wizard-links" ]; then
  # busybox tar does not set a symlink's owner: "uid:gid<NUL>path<NUL>" records from the engine
  (cd "$S" && xargs -0 -n 2 chown -h < .wizard-links)
  rm -f "$S/.wizard-links"
fi
live="$RT/app-data/$STORE/$APP"
if [ -d "$S/app-data" ]; then
  printf '%s\n' "$KEEP" | while IFS= read -r e; do
    [ -n "$e" ] || continue
    if [ -e "$live/$e" ] || [ -L "$live/$e" ]; then
      mkdir -p "$(dirname "$S/app-data/$e")"
      rm -rf "$S/app-data/$e"
      mv "$live/$e" "$S/app-data/$e"
    fi
  done
fi
swap() {
  [ -e "$1" ] || return 0
  mkdir -p "$(dirname "$2")"
  if [ -e "$2" ] || [ -L "$2" ]; then rm -rf "$2.wizard-old"; mv "$2" "$2.wizard-old"; fi
  mv "$1" "$2"
  rm -rf "$2.wizard-old"
}
swap "$S/app-data" "$live"
swap "$S/app" "$RT/apps/$STORE/$APP"
swap "$S/user-config" "$RT/user-config/$STORE/$APP"
for v in $VOLS; do
  [ -d "$S/volumes/$v" ] || continue
  find "$V/$v" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  (cd "$S/volumes/$v" && tar -cf - .) | tar -xpf - --numeric-owner -C "$V/$v"
done
echo restored
'''


class AppData:
    def __init__(self, box) -> None:
        self.box = box
        self._image: str | None = None

    # ------------------------------------------------------------ facts
    def image(self) -> str:
        if self.box.env.get("WAM_BACKUP_HELPER_IMAGE"):
            return self.box.env["WAM_BACKUP_HELPER_IMAGE"]
        if self._image is None:
            p = self._run(["docker", "inspect", "--format", "{{.Config.Image}}", "runtipi"])
            img = p.strip().splitlines()[0] if p.strip() else ""
            if not img:
                raise AppDataError("the Runtipi container (runtipi) was not found: is the App Manager running?")
            self._image = img
        return self._image

    def paths(self, store: str, app_id: str) -> dict[str, Path]:
        rt = self.box.runtipi_root
        return {"app-data": rt / "app-data" / store / app_id, "app": rt / "apps" / store / app_id,
                "user-config": rt / "user-config" / store / app_id}

    def volumes(self, store: str, app_id: str) -> list[str]:
        names = compose_volumes(self.paths(store, app_id)["app"], f"{app_id}_{store}")
        return [n for n in names if self.volume_exists(n)]

    def volume_exists(self, name: str) -> bool:
        try:
            self._run(["docker", "volume", "inspect", name])
            return True
        except AppDataError:
            return False

    def keep_in_place(self, store: str, app_id: str) -> list[str]:
        """Parts of this app's data that stay on this box (relative to its app-data folder): fixed per app, plus the
        agents root when it lives inside this app's data."""
        keep = list(KEEP_IN_PLACE.get(app_id, ()))
        ad = self.paths(store, app_id)["app-data"]
        try:
            rel = self.box.agents_root().relative_to(ad).as_posix()
            if rel not in keep and rel != ".":
                keep.append(rel)
        except ValueError:
            pass
        return keep

    def _run(self, argv: list[str], timeout: int = 120) -> str:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=self.box.env,
                               stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise AppDataError("docker is not installed on this box") from None
        except subprocess.TimeoutExpired:
            raise AppDataError(f"docker {argv[1]} timed out") from None
        if p.returncode:
            raise AppDataError(f"docker {argv[1]} failed: {(p.stderr or p.stdout).strip()[-300:]}")
        return p.stdout

    # ------------------------------------------------------------ archive
    def archive(self, store: str, app_id: str, out: Path, *, volumes: list[str] | None = None) -> dict:
        """Archive the app (stopped by the caller) into `out` (tar.gz) with `volumes` (default: every named volume
        its compose file declares that exists). Returns {entries, bytes, volumes, skipped, kept_in_place}."""
        paths = {k: p for k, p in self.paths(store, app_id).items() if p.is_dir()}
        if not paths:
            raise AppDataError(f"{app_id} has no folders under Runtipi")
        vols = list(self.volumes(store, app_id) if volumes is None else volumes)
        return self._tar(paths, vols, self.keep_in_place(store, app_id), out, app_id)

    def archive_volumes(self, volumes: list[str], out: Path) -> dict:
        """Only these named volumes, as volumes/<name>/ (an undo copy of volumes an import overwrites)."""
        return self._tar({}, list(volumes), [], out, "volumes " + ", ".join(volumes))

    def _tar(self, paths: dict[str, Path], vols: list[str], keep: list[str], out: Path, what: str) -> dict:
        for v in vols:
            if not VOLUME_NAME.match(v):
                raise AppDataError(f"bad volume name {v[:60]!r}")
        cmd = ["docker", "run", "--rm", "--network", "none", "-u", "0:0"]
        for k, p in paths.items():
            cmd += ["-v", f"{p}:/w/{k}:ro"]
        for v in vols:
            cmd += ["-v", f"{v}:/w/volumes/{v}:ro"]
        if "app-data" in paths:
            for rel in keep:
                if (paths["app-data"] / rel).is_dir() and not (paths["app-data"] / rel).is_symlink():
                    cmd += ["--mount", f"type=tmpfs,destination=/w/app-data/{rel}"]
        cmd += ["--entrypoint", "tar", self.image(), "-cf", "-", "-C", "/w", *paths, *(["volumes"] if vols else [])]
        skipped: list[str] = []
        kept_out = ["app-data/" + k for k in keep]
        g = MemberGuard()
        out = Path(out)
        part = out.with_name(out.name + ".part")
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL, env=self.box.env)
            try:
                fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz", compresslevel=6) as w, \
                        tarfile.open(fileobj=proc.stdout, mode="r|") as t:
                    for m in t:
                        name = _clean(m.name)
                        if any(name == k or name.startswith(k + "/") for k in kept_out):
                            continue
                        reason = _skip_reason(m, name, g)
                        if reason:
                            skipped.append(f"{name} ({reason})")
                            continue
                        name = g.check(m)
                        if name is None:
                            continue
                        m.name = name
                        if m.islnk():
                            m.linkname = _clean(m.linkname)
                        m.uname = m.gname = ""
                        w.addfile(m, t.extractfile(m) if m.isreg() else None)
                rc = proc.wait()
            except BaseException:
                proc.kill()
                proc.wait()
                part.unlink(missing_ok=True)
                raise
            err.seek(0)
            msgs = err.read().decode(errors="replace").splitlines()
        sockets = [x for x in msgs if x.rstrip().endswith("socket ignored")]
        other = [x for x in msgs if x not in sockets and x.strip()]
        for x in sockets:   # busybox: "tar: ./app-data/x/gateway.sock: socket ignored"
            body = x.split(":", 1)[1] if ":" in x else x
            skipped.append(_clean(body.rsplit(":", 1)[0].strip()) + " (socket)")
        if rc not in (0, 1) or (rc == 1 and other):
            part.unlink(missing_ok=True)
            raise AppDataError(f"archiving {what} failed: {' '.join(other)[-300:] or f'tar exit {rc}'}")
        os.replace(part, out)
        return {"entries": g.entries, "bytes": g.bytes, "volumes": vols, "skipped": skipped, "kept_in_place": keep}

    def _helper(self, mounts: list[str], env: dict[str, str], script: str, stdin_tar=None) -> None:
        cmd = ["docker", "run", "--rm", "-i", "--network", "none", "-u", "0:0"]
        for mnt in mounts:
            cmd += ["-v", mnt]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += ["--entrypoint", "sh", self.image(), "-c", script]
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err, env=self.box.env)
            try:
                if stdin_tar is not None:
                    stdin_tar(proc.stdin)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            rc = proc.wait()
            err.seek(0)
            msg = err.read().decode(errors="replace").strip()
        if rc:
            raise AppDataError(msg[-300:] or f"exit {rc}")


    def place_package(self, store: str, app_id: str, src: Path) -> None:
        """Copy a bundled Runtipi package into repos/<store>/apps/<id> (root-owned, as Runtipi's own entries are).
        Regular files and folders only."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", app_id) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", store):
            raise AppDataError(f"bad app id {app_id!r}")

        def feed(stdin):
            with tarfile.open(fileobj=stdin, mode="w|") as w:
                for dp, dns, fns in os.walk(src):
                    dns.sort()
                    for n in sorted(dns + fns):
                        q = Path(dp) / n
                        if q.is_symlink() or not (q.is_dir() or q.is_file()):
                            continue
                        ti = w.gettarinfo(str(q), q.relative_to(src).as_posix())
                        ti.uid = ti.gid = 0
                        ti.uname = ti.gname = ""
                        if ti.isreg():
                            with open(q, "rb") as f:
                                w.addfile(ti, f)
                        else:
                            w.addfile(ti)
        self._helper([f"{self.box.runtipi_root / 'repos'}:/repos"], {"STORE": store, "APP": app_id}, PLACE_SH, feed)


    def remove_package(self, store: str, app_id: str) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", app_id) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", store):
            raise AppDataError(f"bad app id {app_id!r}")
        self._helper([f"{self.box.runtipi_root / 'repos'}:/repos"], {"STORE": store, "APP": app_id},
                     'rm -rf "/repos/$STORE/apps/$APP"')

    # ------------------------------------------------------------ restore
    def restore(self, store: str, app_id: str, archive: Path, *, parts=ROOTS) -> dict:
        """Put an archive back (the app stopped by the caller). `parts` limits which roots are replaced."""
        parts = set(parts)
        keep = self.keep_in_place(store, app_id)
        vols: list[str] = []
        with tarfile.open(archive, "r|gz") as t:
            for m in t:
                n = _clean(m.name).split("/")
                if n[0] == "volumes" and len(n) > 1 and n[1] not in vols:
                    vols.append(n[1])
        vols = [v for v in vols if "volumes" in parts]
        for v in vols:
            if not VOLUME_NAME.match(v):
                raise UnsafeMember(f"bad volume name {v[:60]!r}")
        cmd = ["docker", "run", "--rm", "-i", "--network", "none", "-u", "0:0",
               "-v", f"{self.box.runtipi_root}:/rt", "-e", "RT=/rt", "-e", "V=/v", "-e", f"STORE={store}",
               "-e", f"APP={app_id}", "-e", "KEEP=" + "\n".join(keep), "-e", "VOLS=" + " ".join(vols)]
        for v in vols:
            cmd += ["-v", f"{v}:/v/{v}"]
        cmd += ["--entrypoint", "sh", self.image(), "-c", RESTORE_SH]
        g = MemberGuard()
        kept_out = ["app-data/" + k for k in keep]
        links: list[bytes] = []
        with tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as outf:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=outf, stderr=err, env=self.box.env)
            failed: BaseException | None = None
            try:
                with tarfile.open(archive, "r|gz") as t, tarfile.open(fileobj=proc.stdin, mode="w|") as w:
                    for m in t:
                        name = g.check(m)
                        if name is None:
                            continue
                        top = link_root(name).split("/")[0]
                        if top not in parts or any(name == k or name.startswith(k + "/") for k in kept_out):
                            continue
                        m.name = name
                        if m.islnk():
                            m.linkname = _clean(m.linkname)
                        m.uname = m.gname = ""
                        if m.issym():
                            links.append(f"{int(m.uid)}:{int(m.gid)}".encode() + b"\0" + name.encode(
                                "utf-8", "surrogateescape") + b"\0")
                        w.addfile(m, t.extractfile(m) if m.isreg() else None)
                    if links:
                        data = b"".join(links)
                        ti = tarfile.TarInfo(".wizard-links")
                        ti.size, ti.mode = len(data), 0o600
                        w.addfile(ti, io.BytesIO(data))
            except (BrokenPipeError, ConnectionResetError) as e:
                failed = e
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            rc = proc.wait()
            err.seek(0)
            outf.seek(0)
            msg = err.read().decode(errors="replace").strip()
            done = b"restored" in outf.read()
        if rc or failed or not done:
            raise AppDataError(f"putting back {app_id} failed: {msg[-300:] or failed or f'exit {rc}'}")
        return {"entries": g.entries, "bytes": g.bytes, "volumes": vols, "kept_in_place": keep}


PLACE_SH = r'''
set -eu
D="/repos/$STORE/apps"
mkdir -p "$D"
rm -rf "$D/.$APP.wizard-new"
mkdir "$D/.$APP.wizard-new"
tar -xf - -C "$D/.$APP.wizard-new"
[ ! -e "$D/$APP" ] || { rm -rf "$D/.$APP.wizard-new"; echo "exists" >&2; exit 3; }
mv "$D/.$APP.wizard-new" "$D/$APP"
echo placed
'''


def _skip_reason(m: tarfile.TarInfo, name: str, g: MemberGuard) -> str | None:
    """Members the archiver leaves out (reported), instead of failing the whole export."""
    if name in ("", "."):
        return None
    if m.ischr() or m.isblk() or m.isfifo():
        return "device or FIFO"
    if m.issym() and not link_inside(name, m.linkname):
        return f"link to {m.linkname[:80]} outside its folder"
    if m.islnk() and _clean(m.linkname) not in g.files:
        return "hard link to a skipped file"
    parts = name.split("/")
    for i in range(1, len(parts)):
        if "/".join(parts[:i]) in g.links:
            return "below a link"
    return None


__all__ = ["AppData", "AppDataError", "UnsafeMember", "MemberGuard", "check_archive", "compose_volumes", "PARTS",
           "ROOTS", "link_inside"]
