"""Additional browser-possession gate; network identity is not a browser principal."""
from __future__ import annotations

from dataclasses import dataclass
import hmac
import os
from pathlib import Path
import re

HEADER = 'X-Wizard-Shell-Token'


@dataclass(frozen=True)
class BrowserBoundary:
    enabled: bool = False
    token: str = ''

    def __post_init__(self):
        if self.enabled and not re.fullmatch(r'[a-f0-9]{64}', self.token):
            raise ValueError('Shell token must be 32 random bytes encoded as lowercase hex')

    def allows(self, headers) -> bool:
        if not self.enabled:
            return True
        if headers.get('Origin') == 'null' or headers.get('Sec-Fetch-Site') == 'cross-site':
            return False
        supplied = headers.get(HEADER, '')
        # Header values must be ASCII before compare_digest (which rejects Unicode).
        return bool(isinstance(supplied, str) and re.fullmatch(r'[a-f0-9]{64}', supplied)
                    and hmac.compare_digest(supplied, self.token))


def from_env(env=None) -> BrowserBoundary:
    env = os.environ if env is None else env
    if env.get('WIZARD_BROWSER_BOUNDARY') != '1':
        return BrowserBoundary()
    try:
        token = Path(env['WIZARD_SHELL_TOKEN_FILE']).read_text().strip()
    except (KeyError, OSError):
        raise ValueError('Browser boundary requires a readable WIZARD_SHELL_TOKEN_FILE') from None
    return BrowserBoundary(True, token)
