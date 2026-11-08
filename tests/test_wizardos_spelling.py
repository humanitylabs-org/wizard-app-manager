#!/usr/bin/env python3
"""WizardOS is one word, always (James, 8 Nov 2026): no tracked text spells the product name as two words.

The lowercase identifier wizard-os (repo, paths, slugs, ids) is fine, as is anything joined to a path, id or a
longer word (humanitylabs-org/wizard-os, wizard-os-home, wizard_os, ~/.wizard-os). Display spellings with a
space, a hyphen or a dash between the two words are not. Same rule as wizard-agent-service
server/product-name.mjs and wizard-ux tests/wizardos-name.test.mjs.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VARIANT = re.compile(
    r"(?:(?<![\w/.~-])|(?<=\\[ntr]))wizard(?:[ \t\u00a0\u2009\u202f]+|[ \t\u00a0]*[-\u2010\u2011\u2013\u2014][ \t\u00a0]*)os(?![\w/-])",
    re.IGNORECASE)
# Captured evidence records are kept exactly as captured.
SKIP = re.compile(r"(^|/)evidence/")


def misspells(line: str) -> bool:
    return any(m.group(0) != "wizard-os" for m in VARIANT.finditer(line))


def hits():
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True).stdout
    out = []
    for name in filter(None, files.decode().split("\0")):
        if SKIP.search(name):
            continue
        path = ROOT / name
        try:
            if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue
        text = raw.decode("utf-8", "replace")
        out += [f"{name}:{n}: {line.strip()[:160]}" for n, line in enumerate(text.split("\n"), 1) if misspells(line)]
    return out


class WizardOSNameTests(unittest.TestCase):
    def test_rule_keeps_identifiers(self):
        two = "Wizard" + " OS"
        for bad in (two, two.lower(), "\\n" + two, "Wizard-OS", "Wizard \u2014 OS", f"the {two} app", f"{two}'s"):
            self.assertTrue(misspells(bad), bad)
        for ok in ("WizardOS", "wizard-os", "humanitylabs-org/wizard-os", "wizard-os-home", "wizard_os",
                   "~/.wizard-os", "WIZARD_OS_HOME", "wizardos.app"):
            self.assertFalse(misspells(ok), ok)

    def test_no_tracked_text_spells_the_product_as_two_words(self):
        self.assertEqual(hits(), [], "The product name is WizardOS, one word")


if __name__ == "__main__":
    unittest.main()
