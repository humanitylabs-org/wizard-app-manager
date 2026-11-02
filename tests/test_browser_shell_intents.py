"""Shell intents (wizard-os spec/browser-trust-shell-intents): reviewed ChatGPT/Claude operation ids, owner-confirm
consent and the narrow sign-in navigation contract are WAM review facts, never app claims (default-off)."""
import pytest

from wam import browser_admission as ba

from test_browser_admission import box, install  # noqa: F401  (pytest fixture + helper)

AI_OPS = ['ai_accounts.change', 'ai_accounts.inventory', 'ai_accounts.progress', 'ai_accounts.step']
CHATGPT = """browser_contract:
  version: 1
  isolation: opaque
  operations: [ai_accounts.inventory, ai_accounts.change, ai_accounts.progress, ai_accounts.step]
  navigation:
  - {id: signin, url: 'https://auth.openai.com/codex/device'}
"""


def contract(ops, nav=None):
    raw = {'version': 1, 'isolation': 'opaque', 'operations': ops}
    if nav is not None:
        raw['navigation'] = nav
    return ba.normalize_contract({'browser_contract': raw})


def test_reviewed_ids_and_consent():
    assert {o for o in ba.OPERATIONS if o.startswith(('ai_accounts.', 'claude_code.'))} == set(AI_OPS) | {
        'claude_code.status', 'claude_code.connect', 'claude_code.code', 'claude_code.cancel', 'claude_code.logout', 'claude_code.test'}
    owner = sorted(o for o, d in ba.OPERATIONS.items() if d.get('consent') == 'owner')
    assert owner == ['ai_accounts.change', 'claude_code.connect', 'claude_code.logout']
    assert all(ba.OPERATIONS[o]['scope'] == 'self' and 'slot' not in ba.OPERATIONS[o] for o in AI_OPS)


def test_navigation_normalized_and_part_of_contract():
    c = contract(AI_OPS, [{'id': 'signin', 'url': 'https://claude.ai/oauth/authorize',
                           'query': ['state', 'client_id', 'code_challenge', 'code_challenge_method', 'response_type',
                                     'redirect_uri', 'scope', 'code']}])['contract']
    assert c['navigation'] == [{'id': 'signin', 'url': 'https://claude.ai/oauth/authorize', 'query': sorted(
        ['state', 'client_id', 'code_challenge', 'code_challenge_method', 'response_type', 'redirect_uri', 'scope', 'code'])}]
    assert 'navigation' not in contract(AI_OPS)['contract']   # absent stays absent (old digests unchanged)


@pytest.mark.parametrize('nav, why', [
    ([{'id': 'signin', 'url': 'http://auth.openai.com/codex/device'}], 'https'),
    ([{'id': 'signin', 'url': 'https://*.openai.com/codex/device'}], 'wildcard'),
    ([{'id': 'signin', 'url': 'https://auth.openai.com/codex/device?x=1'}], 'query in url'),
    ([{'id': 'signin', 'url': 'https://auth.openai.com/codex#f'}], 'fragment'),
    ([{'id': 'signin', 'url': 'https://user@auth.openai.com/codex'}], 'userinfo'),
    ([{'id': 'signin', 'url': 'https://auth.openai.com:8443/codex'}], 'port'),
    ([{'id': 'signin', 'url': 'https://127.0.0.1/codex'}], 'ip literal'),
    ([{'id': 'signin', 'url': 'javascript:alert(1)'}], 'scheme'),
    ([{'id': 'signin', 'url': 'https://auth.openai.com/x', 'query': ['access_token']}], 'token name'),
    ([{'id': 'signin', 'url': 'https://auth.openai.com/x', 'query': ['refresh_token']}], 'token name'),
    ([{'id': 'signin', 'url': 'https://auth.openai.com/x', 'query': ['session_id']}], 'session name'),
    ([{'id': 'signin', 'url': 'https://auth.openai.com/x', 'target': '_top'}], 'unknown key'),
    ([{'id': 'a', 'url': 'https://a.example/x'}, {'id': 'a', 'url': 'https://b.example/x'}], 'duplicate id'),
    ([{'id': f'n{i}', 'url': 'https://a.example/x'} for i in range(5)], 'too many'),
])
def test_navigation_refusals(nav, why):
    assert 'error' in contract(AI_OPS, nav), why


def test_navigation_needs_a_signin_operation():
    out = contract(['ai_accounts.inventory'], [{'id': 'signin', 'url': 'https://auth.openai.com/codex/device'}])
    assert 'needs an operation that starts a sign-in' in out['error']


def test_navigation_change_is_a_permission_delta(box):  # noqa: F811
    box.rows.append(install(box.root, 'wizard-chatgpt', 'oscarhenrycollins/chatgpt-connection', contract=CHATGPT,
                            provides='ai-connection'))
    b = box.app(box.write(), 'wizard-chatgpt')
    assert b['contract']['operations'] == AI_OPS
    assert b['contract']['navigation'] == [{'id': 'signin', 'url': 'https://auth.openai.com/codex/device', 'query': []}]
    before = b['permission_revision']
    box.rows[:] = [install(box.root, 'wizard-chatgpt', 'oscarhenrycollins/chatgpt-connection',
                           contract=CHATGPT.replace('https://auth.openai.com/codex/device', 'https://auth.openai.com/codex/device2'),
                           provides='ai-connection')]
    after = box.app(box.write(), 'wizard-chatgpt')
    assert after['contract_digest'] != b['contract_digest'] and after['permission_revision'] != before


def test_unknown_or_hostile_navigation_is_never_projected(box):  # noqa: F811
    hostile = CHATGPT.replace("- {id: signin, url: 'https://auth.openai.com/codex/device'}",
                              "- {id: signin, url: 'https://evil.example/x'}\n  - {id: two, url: 'http://evil.example/'}")
    box.rows.append(install(box.root, 'wizard-chatgpt', 'oscarhenrycollins/chatgpt-connection', contract=hostile,
                            provides='ai-connection'))
    b = box.app(box.write(), 'wizard-chatgpt')
    assert b['reason'] == 'invalid_contract' and b['contract'] is None and b['admissible'] is False
