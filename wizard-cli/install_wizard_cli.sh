#!/bin/sh
# Install the generic Wizard OS CLI (tools/wizard) as a single file. No dependencies beyond python3 >= 3.9.
#
#   sh tools/install_wizard_cli.sh [--prefix DIR] [--name NAME]
#
# Default: $HOME/.local/bin/wizard. Re-running upgrades a copy this script installed. It never overwrites a
# different program already called NAME in DIR: it stops, and you pick another --name (e.g. wizard-os).
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
src="$here/wizard"
prefix="${HOME}/.local/bin"
name="wizard"
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) prefix="$2"; shift 2 ;;
    --name) name="$2"; shift 2 ;;
    -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install_wizard_cli: unknown option $1" >&2; exit 2 ;;
  esac
done
py="${PYTHON:-python3}"
if ! "$py" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  echo "install_wizard_cli: needs python3 3.9 or newer" >&2; exit 1
fi
dest="$prefix/$name"
if [ -e "$dest" ] && ! grep -q 'wizard-os-generic-cli' "$dest" 2>/dev/null; then
  echo "install_wizard_cli: $dest exists and is not the Wizard OS CLI; left it alone." >&2
  echo "  Install under another name instead: sh $0 --name wizard-os" >&2
  exit 1
fi
mkdir -p "$prefix"
tmp="$dest.tmp.$$"
cp "$src" "$tmp"
chmod 0755 "$tmp"
mv -f "$tmp" "$dest"
"$dest" --version
found=$(command -v "$name" 2>/dev/null || true)
if [ -z "$found" ]; then
  echo "note: $prefix is not on PATH; run $dest or add $prefix to PATH" >&2
elif [ "$found" != "$dest" ]; then
  echo "note: '$name' on PATH resolves to $found first, not $dest" >&2
fi
