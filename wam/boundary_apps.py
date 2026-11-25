"""Do the installed app windows still open inside the browser boundary? (wizard-app-manager#1043, wizard-os#133)

With the boundary on, every app window is a sandboxed frame (an opaque origin). The browser then asks for the
page's own files from that frame: `Sec-Fetch-Site: cross-site`, no Referer, and for a module script, a
`modulepreload` or any file marked `crossorigin` also `Origin: null`, which needs an
`Access-Control-Allow-Origin` answer. An app that answers none of that opens blank (TG style, Files on 25 Nov).

`check()` asks each installed app with a window, on its loopback port, the way the shell asks today and the way the
sandboxed window would. An app fails only when a file that loads today would not load in the sandbox, so an app
that refuses this box-local request anyway (an owner login it needs, a page that is not there) is "not checked",
never "blank". Only GET requests, only the app's own page and the files it names; nothing is written.

The installer runs it before it switches the boundary on (cli.settle_existing_browser_boundary) and after a new
box's default apps are in (cli.settle_auto_browser_boundary); an install or update on a box with the boundary on
checks that one app (cli.boundary_after_app).

An app the shell opens as its own browser tab under the boundary (OWN_TAB, the same list as wizard-ux
`web/core/browser-boundary.js` OWN_TAB, wizard-ux#1821) is never a sandboxed frame, so it is listed as `own_tab`
and never counted as blank (wizard-app-manager#1046).

An app the shell keeps same-origin under the boundary (SAME_ORIGIN, the same list as wizard-ux
`web/core/browser-boundary.js` SAME_ORIGIN: TG style from its own listing, humanitylabs-org/telegram#68) is not a
sandboxed frame either: it opens as it did before the boundary, so it is listed as `same_origin` and never counted as
blank. Only by app id AND the repo apps.json says it came from, AND only while the shell installed on this box has
that list (its served core/browser-boundary.js names the app in SAME_ORIGIN): a box whose shell is older still frames
the app sealed, so there it is probed like any app, as is a copy from anywhere else.
"""
from __future__ import annotations

import http.client
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

TIMEOUT = 6
MAX_FILES = 24
MAX_READ = 1 << 20          # enough of any page to find its script and style tags
APP_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Served by the shell or the Agent Service on the box origin, never by the app itself.
SHELL_PATHS = ("/wizard-os-home/", "/wizard/", "/wizard-os/")
CORS_OK = {"*", "null"}
# Installed app id -> the window path under which the shell opens it as its own tab while the boundary is on,
# instead of a sandboxed frame. Keep in step with wizard-ux web/core/browser-boundary.js OWN_TAB (wizard-ux#1821).
OWN_TAB = {"files": "/files/"}
# Installed app id -> (window path, the repo it must be installed from): the shell keeps its window same-origin under
# the boundary. Keep in step with wizard-ux web/core/browser-boundary.js SAME_ORIGIN (humanitylabs-org/telegram#68).
SAME_ORIGIN = {"tg-style": ("/tg-style/", "humanitylabs-org/telegram")}


def own_tab(app_id: str, path: str) -> bool:
    """Whether the shell opens this app in its own tab under the boundary (the shell's `ownTab(appId, url)`)."""
    home = OWN_TAB.get(app_id) if isinstance(app_id, str) else None
    return bool(home) and str(path or "").startswith(home)


def same_origin(app_id: str, path: str, repo) -> bool:
    """Whether the shell keeps this app's window same-origin under the boundary (the shell's `sameOriginApp`)."""
    home, want = SAME_ORIGIN.get(app_id, (None, None)) if isinstance(app_id, str) else (None, None)
    return bool(home) and repo == want and str(path or "").startswith(home) and "//" not in str(path)


class _Tags(HTMLParser):
    """The files a page loads: (url, cors) for each script src and each stylesheet / (module)preload link."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.files: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and a.get("src") and "nomodule" not in a:   # a modern browser skips nomodule files
            self.files.append((a["src"], a.get("type", "").strip().lower() == "module" or "crossorigin" in a))
        elif tag == "link" and a.get("href"):
            rel = set(a.get("rel", "").lower().split())
            if "modulepreload" in rel:
                self.files.append((a["href"], True))
            elif "stylesheet" in rel or ("preload" in rel and a.get("as", "").lower() in ("script", "style", "font")):
                self.files.append((a["href"], "crossorigin" in a or a.get("as", "").lower() == "font"))


def page_files(html: str, page_url: str, prefix: str) -> list[tuple[str, bool]]:
    """The app's own files the page loads, as absolute loopback URLs: same host and port as the page, under the
    app's path, never a shell path."""
    p = _Tags()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - a page the parser chokes on: whatever it found so far
        pass
    host = urlsplit(page_url).netloc
    out, seen = [], set()
    for ref, cors in p.files:
        url = urljoin(page_url, ref.strip())
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or u.netloc != host:
            continue
        if any(u.path.startswith(s) for s in SHELL_PATHS) or (prefix != "/" and not u.path.startswith(prefix)):
            continue
        key = (u.path + ("?" + u.query if u.query else ""), cors)
        if key in seen:
            continue
        seen.add(key)
        out.append((url, cors))
    return out[:MAX_FILES]


def _get(url: str, headers: dict) -> tuple[int, dict, bytes]:
    """One GET, no redirects followed: (status, lower-cased headers, up to MAX_READ bytes). Status 0: no answer."""
    u = urlsplit(url)
    conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(
        u.hostname, u.port, timeout=TIMEOUT)
    try:
        conn.request("GET", (u.path or "/") + ("?" + u.query if u.query else ""), headers=headers)
        r = conn.getresponse()
        body = r.read(MAX_READ)
        return r.status, {k.lower(): v for k, v in r.getheaders()}, body
    except (OSError, http.client.HTTPException):
        return 0, {}, b""
    finally:
        conn.close()


def _frame_headers(url: str, proof) -> dict:
    """The page itself: the shell (same origin) navigates the frame, so this is the same with the boundary on."""
    return {"Accept": "text/html,*/*", "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin", **proof(url)}


def _file_headers(url: str, cors: bool, sandbox: bool, page_url: str, proof) -> dict:
    h = {"Accept": "*/*", "Sec-Fetch-Mode": "cors" if cors else "no-cors",
         "Sec-Fetch-Dest": "script" if re.search(r"\.m?js(\?|$)", url) else "style" if ".css" in url else "empty",
         "Sec-Fetch-Site": "cross-site" if sandbox else "same-origin", **proof(url)}
    if sandbox:
        if cors:
            h["Origin"] = "null"   # an opaque document sends no Referer at all
    else:
        h["Referer"] = page_url
    return h


def _ok(status: int) -> bool:
    return 200 <= status < 300 or status == 304


def probe(app: dict, get=_get) -> dict:
    """{id, name, state: ok | blank | unchecked, why, files}. `app`: {id, name, page, prefix, proof}."""
    base = {"id": app["id"], "name": app.get("name") or app["id"]}
    proof = app.get("proof") or (lambda path: {})
    page = app["page"]
    for _ in range(4):   # an app behind a trailing-slash or login-free redirect on its own port
        st, hd, body = get(page, _frame_headers(page, proof))
        loc = hd.get("location") or ""
        if st in (301, 302, 303, 307, 308) and loc:
            nxt = urljoin(page, loc)
            if urlsplit(nxt).netloc != urlsplit(page).netloc:
                break
            page = nxt
            continue
        break
    if not st:
        return {**base, "state": "unchecked", "down": True, "why": "its page did not answer (stopped or still starting)"}
    if not _ok(st) or "html" not in (hd.get("content-type") or "text/html").lower():
        return {**base, "state": "unchecked", "why": f"its page answers HTTP {st} to a box-local request"}
    files = page_files(body.decode("utf-8", "replace"), page, app.get("prefix") or "/")
    bad = []
    for url, cors in files:
        st0, _, _ = get(url, _file_headers(url, cors, False, page, proof))
        if not _ok(st0):
            continue   # does not load today either: not something the boundary changes
        st1, h1, _ = get(url, _file_headers(url, cors, True, page, proof))
        path = urlsplit(url).path
        if not _ok(st1):
            bad.append(f"{path} (HTTP {st1 or 'no answer'} in a sandboxed window)")
        elif cors and (h1.get("access-control-allow-origin") or "").strip() not in CORS_OK:
            bad.append(f"{path} (no Access-Control-Allow-Origin for Origin: null)")
    if bad:
        return {**base, "state": "blank", "files": bad,
                "why": f"{len(bad)} of its page files would not load in a sandboxed window: " + "; ".join(bad[:3])}
    return {**base, "state": "ok", "files": [urlsplit(u).path for u, _ in files]}


# The shell's own boundary code, as the shell serves it (the box front mounts the shell at /wizard-os-home/).
SHELL_BOUNDARY_JS = ("/wizard-os-home/core/browser-boundary.js", "/core/browser-boundary.js")


def shell_same_origin(home, runtipi_root, shell_ids, get=None) -> set:
    """The app ids the installed shell keeps same-origin under the boundary: read from its served
    core/browser-boundary.js (its SAME_ORIGIN list), on the shell's loopback port. Empty when the shell is not found
    or older (no list), so such an app is checked like any other."""
    from . import app_switches
    get = get or _get
    try:
        reg = json.loads((Path(home) / "apps.json").read_text())
    except (OSError, ValueError):
        return set()
    for a in reg.get("apps") or []:
        if not isinstance(a, dict) or a.get("id") not in set(shell_ids) or not APP_ID.fullmatch(str(a.get("id"))):
            continue
        local = app_switches.loopback(a, Path(runtipi_root), _store_of(a))
        if not local:
            continue
        for path in SHELL_BOUNDARY_JS:
            status, _, body = get(local.rstrip("/") + path, {"Accept": "*/*"})
            if status != 200:
                continue
            m = re.search(rb"const SAME_ORIGIN = Object\.freeze\(\{(.*?)\}\);", body, re.S)
            if not m:
                return set()
            return {i for i in SAME_ORIGIN if re.search(rb"['\"]" + re.escape(i.encode()) + rb"['\"]\s*:", m.group(1))}
    return set()


def _store_of(a: dict) -> str:
    store = (str(a.get("urn") or "").split(":", 1) + ["wizardapps"])[1] or "wizardapps"
    return store if APP_ID.fullmatch(store) else "wizardapps"


def _container_env(app_id: str, store: str) -> dict:
    """The running app container's environment (Runtipi's compose project `<id>_<store>`), or {}."""
    import subprocess
    try:
        from .network import docker_cmd
        docker = docker_cmd()
        ids = subprocess.run(docker + ["ps", "-q", "--filter", f"label=com.docker.compose.project={app_id}_{store}"],
                             capture_output=True, text=True, timeout=15).stdout.split()
        if not ids:
            return {}
        raw = subprocess.run(docker + ["inspect", "--format", "{{json .Config.Env}}", *ids[:4]],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001 - discovery only
        return {}
    out = {}
    for line in raw.splitlines():
        try:
            for kv in json.loads(line) or []:
                k, _, v = str(kv).partition("=")
                if v and k not in out:
                    out[k] = v
        except ValueError:
            continue
    return out


def _box_host(home: Path, root: Path, store: str, app_id: str) -> str | None:
    """The host the browser names when it opens this app: the first of the app's WIZARD_TRUSTED_HOSTS (what
    `wam` gave it, cli.trusted_origin_env), else the box's own shell origin, else None (the loopback address)."""
    from .runtipi import read_env
    found = [_container_env(app_id, store).get("WIZARD_TRUSTED_HOSTS") or ""]
    for p in (root / "user-config" / store / app_id / "app.env", root / "app-data" / store / app_id / "app.env"):
        try:
            found.append(read_env(p).get("WIZARD_TRUSTED_HOSTS") or "")
        except (OSError, ValueError):
            continue
    for raw in found:
        got = raw.split(",")[0].strip()
        if re.fullmatch(r"[A-Za-z0-9.-]+(:\d{1,5})?", got):
            return got
    try:
        from .app_switches import box_origin
        o = box_origin(home)
    except Exception:  # noqa: BLE001 - discovery only
        o = None
    return urlsplit(o).netloc if o else None


def windows(home, runtipi_root, skip=(), only=None) -> list[dict]:
    """Installed apps with a window, from apps.json: {id, name, page, prefix, proof}. `skip`: the shell and the
    Agent Service themselves (the shell is the top window, not a frame). `only`: just these ids."""
    from . import app_switches, owner_proof
    home, root = Path(home), Path(runtipi_root)
    try:
        reg = json.loads((home / "apps.json").read_text())
    except (OSError, ValueError):
        return []
    out = []
    for a in reg.get("apps") or []:
        if not isinstance(a, dict) or not APP_ID.fullmatch(str(a.get("id") or "")) or a["id"] in set(skip):
            continue
        if only is not None and a["id"] not in set(only):
            continue
        view = a.get("view") if isinstance(a.get("view"), dict) else {}
        if not (view.get("url") or view.get("local")):
            continue   # no window
        local = app_switches.loopback(a, root, _store_of(a))
        if not local:
            continue
        lp = urlsplit(local)
        path = urlsplit(str(view.get("local") or "")).path or urlsplit(str(view.get("url") or "")).path or "/"
        if not path.startswith("/") or ".." in path:
            path = "/"
        path = path if path.endswith("/") else path + "/"
        key = app_switches.proof_key(home, a["id"])
        host = _box_host(home, root, _store_of(a), a["id"])

        def proof(url, _k=key, _id=a["id"], _host=host):
            """What the owner's request carries by the time it reaches the app: the box's own host name (apps
            answer only hosts they trust) and, for an owner-proof app, the edge's signature."""
            u = urlsplit(url)
            h = {"Host": _host} if _host else {}
            if _k:
                h[owner_proof.HEADER] = owner_proof.sign(_k, _id, "GET", (u.path or "/") + ("?" + u.query if u.query else ""))
            return h

        out.append({"id": a["id"], "name": str(a.get("name") or a["id"])[:80],
                    "page": f"{lp.scheme}://{lp.netloc}{path}", "prefix": path, "proof": proof,
                    **({"own_tab": True} if own_tab(a["id"], path) else {}),
                    **({"same_origin": True} if same_origin(a["id"], path, a.get("repo")) else {})})
    return out


def check(home, runtipi_root, skip=(), only=None, get=_get, wait_s: float = 0) -> dict:
    """{ok: [names], blank: [{id, name, why, files}], unchecked: [{id, name, why}], own_tab: [names],
    same_origin: [names]}. `wait_s`: keep retrying apps whose page does not answer yet (just installed or updated)
    until that many seconds have passed in all. `own_tab`: apps the shell opens in their own tab under the boundary
    (OWN_TAB); `same_origin`: apps whose window the shell keeps same-origin (SAME_ORIGIN): neither is a sandboxed
    frame, so neither is probed or ever blank."""
    out = {"ok": [], "blank": [], "unchecked": [], "own_tab": [], "same_origin": []}
    end = time.monotonic() + wait_s
    apps = windows(home, runtipi_root, skip, only)
    kept = shell_same_origin(home, runtipi_root, skip, get) if any(a.get("same_origin") for a in apps) else set()
    for app in apps:
        if app.get("own_tab"):
            out["own_tab"].append(app["name"])
            continue
        if app.get("same_origin") and app["id"] in kept:
            out["same_origin"].append(app["name"])
            continue
        while True:
            r = probe(app, get)
            if not r.get("down") or time.monotonic() >= end:
                break
            time.sleep(2)
        if r["state"] == "ok":
            out["ok"].append(r["name"])
        else:
            out[r["state"]].append({k: r[k] for k in ("id", "name", "why", "files") if k in r})
    return out


def reason(res: dict) -> str | None:
    """The plain reason the boundary stays off, naming the apps, or None when nothing would open blank."""
    if not res.get("blank"):
        return None
    names = ", ".join(b["name"] for b in res["blank"])
    return (f"{names} would open blank in a sandboxed window (their page files don't answer Origin: null); "
            "the boundary stays off and is tried again on the next update")
