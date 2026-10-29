"""Fail-closed classification of the pinned real-Docker baseline negative.

No Docker calls. A formatted diff is not evidence of network identity. The
machine report identifies the exact assertion in hash-pinned source; the saved
inspect inventory and live events independently establish the positive bypass.
This proves presence, not completeness of a no-exposure observation window.
"""
import hashlib
import re

FIXTURE = 'packages/backend/src/modules/docker/__tests__/mandatory-seal.test.ts'
FIXTURE_SHA256 = 'baaafa4c5e8fd7e597890226a9db8aa014a5c34257d1264f2c1a8517069ff14e'
SUITE = 'mandatory seal direct manager lifecycle (real Docker)'
TITLE = 'disabled, removed and malicious optional config cannot attach a transient public network'
ROUNDTRIP = 'round-trips raw overlay through pinned Compose config and snapshot without extra escaping'
COUNTS = {'numFailedTests': 1, 'numPassedTests': 1, 'numPendingTests': 17, 'numTotalTests': 19, 'success': False}
ROLES = {
    'agent-synthetic': ['seal'],
    'agent-synthetic-tinfoil': ['provider', 'keyed'],
    'agent-synthetic-search': ['provider', 'keyed'],
    'agent-synthetic-key': ['seal', 'keyed'],
    'agent-synthetic-edge': ['seal', 'provider', 'edge-uplink'],
}


def baseline_failure(report, trace=None, fixture_source=None, project=None):
    """Require exact-source assertion identity AND correlated positive evidence.

    Only this explicitly reviewed fixture version is supported. Source edits,
    missing evidence, another assertion or arbitrary public attachments refuse.
    expectedNetworkIds is an OBSERVED inspect inventory despite its historic
    name; the allowed role networks come from the hash-pinned fixture instead.
    """
    assert isinstance(fixture_source, bytes) and hashlib.sha256(fixture_source).hexdigest() == FIXTURE_SHA256, 'baseline fixture source identity'
    assert isinstance(project, str) and re.fullmatch(r'agent-synthetic_seal[0-9a-f]{12}', project), 'baseline project identity'
    assert isinstance(trace, dict), 'baseline captured trace required'
    assert {k: report.get(k) for k in COUNTS} == COUNTS, 'baseline active/filter inventory'
    suites = report['testResults']
    assert len(suites) == 1 and suites[0]['name'] == '/work/source/'+FIXTURE and suites[0]['status'] == 'failed', 'baseline suite identity'
    tests = suites[0]['assertionResults']
    assert len(tests) == 19, 'baseline assertion inventory'
    assert [sum(t['status'] == s for t in tests) for s in ('failed', 'passed', 'skipped')] == [1, 1, 17], 'baseline assertion counts'
    failed = next(t for t in tests if t['status'] == 'failed')
    assert failed['ancestorTitles'] == [SUITE] and failed['title'] == TITLE and failed['fullName'] == SUITE+' '+TITLE, 'baseline test identity'
    passed = next(t for t in tests if t['status'] == 'passed')
    assert passed['fullName'] == SUITE+' '+ROUNDTRIP and not passed['failureMessages'], 'baseline roundtrip control'
    messages = failed['failureMessages']
    assert len(messages) == 1 and messages[0].startswith('AssertionError: expected '), 'baseline assertion type'
    # Vitest 4 JSON carries stack frames in failureMessages, not assertionLocation.
    # Parse frame identity, NEVER require rendered array values or _public_app.
    frames = re.findall(r'^\s+at (?:([^()\n]+) \()?(/work/source/[^\s():]+):(\d+):(\d+)\)?$', messages[0], re.MULTILINE)
    assert frames == [('inspect', '/work/source/'+FIXTURE, '579', '59'), ('', '/work/source/'+FIXTURE, '603', '7')], 'baseline exact assertion/callsite'
    lines = fixture_source.decode().splitlines()
    assert lines[578].strip() == 'for (const row of rows) expect(row.networks.sort()).toEqual(Object.keys(doc.services[row.service].networks).map((n) => `${project}_${n}`).sort());'
    assert lines[602].strip() == 'await inspect();'
    assert trace.get('readiness') == 'sentinel-create' and 'error' not in trace and trace['checks'] == [], 'baseline first-inspect trace phase'
    ids = trace['expectedContainers']
    assert len(ids) == len(set(ids)) == 5 and all(re.fullmatch('[0-9a-f]{64}', x) for x in ids), 'baseline exact inspected containers'
    inventory_rows = trace['expectedNetworkIds']
    assert len(inventory_rows) == 5 and len({row[0] for row in inventory_rows}) == 5, 'baseline inspect inventory duplicates'
    inventory = dict(inventory_rows)
    assert set(inventory) == set(ids), 'baseline inspect container closure'
    urn = project.replace('_', ':', 1)
    owner = 'manager-seal-docker-'+project.removeprefix('agent-synthetic_seal')
    creates = {}; sentinels = []; connects = []; roles = {}
    events = trace['events']
    assert isinstance(events, list) and len(events) <= 100, 'baseline bounded trace'
    for index, event in enumerate(events):
        actor = event['Actor']; attrs = actor['Attributes']
        if event['Type'] == 'container' and event['Action'] == 'create':
            cid = actor['ID']
            assert cid not in creates, 'baseline duplicate create'
            assert attrs.get('wizard.qa.manager-seal.fixture') == urn and attrs.get('wizard.qa.manager-seal') == owner, 'baseline owned create identity'
            creates[cid] = index
            if cid in ids:
                role = attrs.get('com.docker.compose.service')
                assert attrs.get('com.docker.compose.project') == project and role in ROLES and role not in roles.values(), 'baseline source role identity'
                roles[cid] = role
            else:
                assert 'com.docker.compose.project' not in attrs, 'baseline unexpected service'
                sentinels.append(cid)
        else:
            assert event['Type'] == 'network' and event['Action'] == 'connect', 'baseline event type'
            assert attrs.get('container') in ids and attrs.get('type') == 'bridge', 'baseline connect container identity'
            connects.append((attrs['container'], attrs['name'], actor['ID'], index))
    assert set(roles) == set(ids) and set(roles.values()) == set(ROLES), 'baseline complete source roles'
    assert len(sentinels) == 1 and creates[sentinels[0]] < min(creates[cid] for cid in ids), 'baseline readiness sentinel'
    observed = {}; names = {}; positive = []
    for cid, name, nid, index in connects:
        assert re.fullmatch('[0-9a-f]{64}', nid), 'baseline full network identity'
        assert name not in observed.setdefault(cid, {}), 'baseline duplicate connect'
        assert name not in names or names[name] == nid, 'baseline inconsistent network identity'
        names[name] = nid; observed[cid][name] = nid
    assert observed == inventory, 'baseline connect/inspect identity mismatch or missing evidence'
    assert len(set(names.values())) == len(names), 'baseline network ID aliases'
    for cid in ids:
        role = roles[cid]
        allowed = {project+'_agent-synthetic-'+suffix for suffix in ROLES[role]}
        actual = set(inventory[cid])
        extra = {project+'_public_app'} | ({project+'_public_main'} if role == 'agent-synthetic' else set())
        assert actual == allowed | extra, 'baseline source role network mismatch'
        positive.append({'container_id': cid, 'service': role, 'allowed_networks': sorted(allowed),
                         'observed_networks': inventory[cid], 'unexpected_networks': sorted(extra)})
    # expectedContainers insertion order is rows order in the pinned source.
    # Therefore the FIRST row is the exact row that throws at line 579.
    first = positive[0]
    assert first['service'] in ('agent-synthetic-search', 'agent-synthetic-tinfoil'), 'baseline first failing row identity'
    assert len(first['observed_networks']) == 3 and len(first['allowed_networks']) == 2, 'baseline first assertion dimensions'
    return {'classification': 'exact-source original disabled-config public-network bypass reproduced',
            'fixture_sha256': FIXTURE_SHA256, 'assertion': {'line': 579, 'column': 59, 'call_line': 603, 'call_column': 7},
            'project': project, 'first_failing_row': first, 'correlated_rows': positive,
            'public_app_network_id': names[project+'_public_app'], 'public_app_connects': len(positive),
            'boundary': 'positive captured attachment only; baseline aborted before final drain/reconciliation; not absence proof or installed API acceptance'}
