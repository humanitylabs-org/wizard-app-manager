#!/usr/bin/env bash
# Run only on the Linux worker, from an immutable staged WAM checkout.
set -uo pipefail
ux="$(realpath "${1:?wizard-ux checkout required}")"
out="$(realpath -m "${2:?evidence directory required}")"
mkdir -p "$out"
export TMPDIR="$out/scratch"
mkdir -p "$TMPDIR"
python3 -m venv .release-qa-venv || exit 1
py="$PWD/.release-qa-venv/bin/python"
"$py" -m pip install pytest pyyaml pyjwt playwright >"$out/dependencies.log" 2>&1 || exit 1
"$py" -m playwright install chromium >"$out/browser-install.log" 2>&1 || exit 1
status=0
"$py" -m pytest -q tests/test_release.py tests/test_release_policy.py tests/test_updates.py --junitxml="$out/wam-unit.xml" >"$out/wam-unit.log" 2>&1 || status=1
(cd "$ux" && node --test tests/*.test.mjs) >"$out/ux-unit.log" 2>&1 || status=1
"$py" tools/qa_release.py --ux "$ux" --out "$out/e2e" >"$out/e2e.log" 2>&1 || status=1
"$py" "$ux/tools/qa_developer_release.py" --out "$out/browser" >"$out/browser.log" 2>&1 || status=1
printf 'acceptance_exit=%s\n' "$status" >"$out/exit-status.txt"
exit "$status"
