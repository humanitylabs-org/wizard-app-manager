"""Compose reads build contexts in Runtipi; daemon bind sources stay on the host."""
from pathlib import Path

import pytest

from wam import packaging

SHA = "b" * 40


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("context", [".", "app"])
def test_build_context_container_path_keeps_host_source(tmp_path, external, context):
    root = tmp_path / "runtipi"
    root.mkdir()
    if external:
        # Includes app-data in a parent name: map the actual source root, not that parent.
        base = tmp_path / "app-data" / "external"
        (root / ".env").write_text(f"RUNTIPI_APP_DATA_PATH={base}\n")
    else:
        base = root
    host = packaging.source_dir(packaging.app_data_root(root), "wizardapps", "demo", SHA)
    assert host == base / "app-data/.wizard-source/wizardapps" / f"demo@{SHA[:12]}"
    src = tmp_path / "checkout"
    (src / "app").mkdir(parents=True)
    (src / "app/Dockerfile").write_text("FROM scratch\n")
    # APP_SOURCE_DIR is the host-side env emitted by app.helpers.ts for daemon binds.
    compose = {"services": {"web": {
        "build": {"context": context, "args": {"KEEP": "yes"}},
        "environment": {"APP_SOURCE_DIR": str(host)},
        "volumes": ["${APP_SOURCE_DIR}/app:/app:ro"],
    }}}
    packaging.prepare_package("build", compose, src, host, "demo", SHA)
    service = compose["services"]["web"]
    expected = f"/app-data/.wizard-source/wizardapps/demo@{SHA[:12]}"
    assert service["build"]["context"] == expected + ("/app" if context == "app" else "")
    assert service["build"]["args"] == {"KEEP": "yes"}
    assert service["environment"]["APP_SOURCE_DIR"] == str(host)
    assert service["volumes"] == ["${APP_SOURCE_DIR}/app:/app:ro"]
    assert (host / "app/Dockerfile").is_file()


def test_backend_keeps_container_context_and_host_bind_namespaces():
    backend = Path(__file__).resolve().parents[1] / "packages/backend/src"
    # Guard the unchanged backend contract alongside the host materializer regression.
    helper = (backend / "modules/apps/app.helpers.ts").read_text()
    assert "envMap.set('APP_SOURCE_DIR', sourceDir(`${userSettings.appDataPath}/app-data`, appStoreId, appName, commit))" in helper
    store = (backend / "modules/app-stores/wizard-index-store.ts").read_text()
    assert "sourceDir(appDataDir," in store
    assert "b.context = ctx ? path.join(target, ctx) : target" in (backend / "modules/app-stores/packaging.ts").read_text()
