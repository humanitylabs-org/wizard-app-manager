"""QA-only single trusted revision + one shipped update. No runtime on import."""
import hashlib
import json
from pathlib import Path
import re
import uuid

URN = 'agent-synthetic:migrated'
COMMAND_V1 = 'import time; time.sleep(1800)'
COMMAND_V2 = COMMAND_V1+' # trusted revision 2'


def source_contract(root):
    """Exact source/line pins; do not mistake snapshot reinput for anchor promotion."""
    root = Path(root)
    checks = {
        'packages/backend/src/modules/docker/mandatory-seal.ts': [
            'if (prior.previous) throw fail();', 'A second distinct revision requires',
            'async stage(', 'snapshot(effective:', 'return JSON.stringify(Object.fromEntries'],
        'packages/backend/src/cli/enroll-mandatory-seal.ts': [
            "['enroll', 'stage']", '? await seal.stage(urn, expected!, compose, catalogue)'],
        'packages/backend/src/modules/docker/docker.service.ts': [
            "const snapshot = path.join(dir, 'effective.json');", 'seal.snapshot(effective)',
            'await launch([...project, \'-f\', snapshot]);', 'await fs.rm(dir, { recursive: true, force: true });'],
        'packages/backend/src/modules/app-lifecycle/app-lifecycle.controller.ts': ["@Patch(':urn/update')"],
        'packages/backend/src/modules/app-lifecycle/dto/app-lifecycle.dto.ts': ["performBackup: 'boolean'"],
        'packages/backend/src/modules/app-lifecycle/commands/update-app-command.ts': [
            'previousAppDirectory = path.join(temporaryDirectory',
            'await dockerService.prepareApp(appUrn);', "await dockerService.composeApp(appUrn, 'pull');",
            "await dockerService.composeApp(appUrn, 'down --remove-orphans');",
            'if (previousFilesStaged && deploymentMayHaveChanged && deploymentMayHaveStopped && this.wasRunningBeforeUpdate)',
            "await dockerService.composeApp(appUrn, 'up --detach --force-recreate --remove-orphans');"],
        'packages/backend/src/modules/app-lifecycle/handlers/update-app.handler.ts': [
            "transitionTo(app.id, appUrn, 'updating')", 'version: updatedVersion',
            'rollbackSucceeded === true ? appStatusBeforeUpdate'],
        'packages/backend/src/modules/docker/wizard-shares.controller.ts': [
            "@Post(':id/app-grants')", 'this.docker.storage.change({ kind: \'app\'',
            "@Post(':id/share-grants')"],
        'packages/backend/src/modules/docker/wizard-storage.ts': [
            "path.join(dir, 'wizard.md')", 'if (!text) continue;',
            "if (!consumer) throw new Error('Consumer not installed');",
            "await fs.writeFile(tmp, `${JSON.stringify(file, null, 2)}\\n`, { mode: 0o600 });"],
    }
    pins = {}
    for rel, tokens in checks.items():
        raw = (root/rel).read_bytes(); lines = raw.decode().splitlines()
        pin = {'sha256': hashlib.sha256(raw).hexdigest(), 'lines': {}}
        for token in tokens:
            found = [i for i, line in enumerate(lines, 1) if token in line]
            assert found, 'update source contract drift: '+rel
            pin['lines'][token] = found
        pins[rel] = pin
    seal = (root/'packages/backend/src/modules/docker/mandatory-seal.ts').read_text()
    update = (root/'packages/backend/src/modules/app-lifecycle/commands/update-app-command.ts').read_text()
    # Full writer inventory is deliberately closed: only enroll and stage write policy.
    assert seal.count("'policy.json.next'") == 1 and seal.count("'policy.json'=") == 0
    assert 'MandatorySeal' not in update and 'previous' not in seal[seal.index('snapshot(effective:'):]
    return {'source_pins': pins, 'update_method': 'PATCH', 'update_body': {'performBackup': False},
            'single_accepted_update_prepared': True, 'runtime_executed': False,
            'rollback_transaction_prepared': False, 'grant_revocation_prepared': False,
            'rollback_gap': 'No trusted installed/promotion transition clears policy.previous after successful update. A second distinct staged pair is refused at mandatory-seal.ts:144. Update recovery copies prior app/env and may recreate runtime, but never clears/promotes the seal anchor. Do not edit policy or inject a guard fault as a substitute for a shipped revision transition.',
            'grant_gap': 'HTTP app-grant revoke exists, but the retained synthetic fixture has no wizard.md and no app grant to revoke. WizardStorage.apps skips it (208-212); change refuses a missing consumer (308-309). A real grant preservation proof needs a separately reviewed manifest/provider/grant fixture, not a no-op revoke or handwritten grant state.',
            'full_acceptance': False, 'RUNTIPI_ADMITTED': False}


def stage(rig, subnet):
    """Review immutable compiler inputs, CAS stage via shipped CLI, then publish recipe."""
    reviewed = json.loads(rig.exec(rig.manager, ['/tooling/node', '/app/review.mjs', 'candidate', subnet]))
    assert reviewed['reviewed'] is True and reviewed['mode'] == 'candidate'
    expected = reviewed['expected_sha256']; assert re.fullmatch('[0-9a-f]{64}', expected)
    result = json.loads(rig.exec(rig.manager, ['/tooling/node', '/app/enroll.mjs', 'stage', '/data', URN,
        '/data/reviewed-update/compiled.json', '/data/reviewed-update/catalogue.json', expected]))
    assert result['previous_sha256'] == expected and result['sha256'] != expected
    assert re.fullmatch('[0-9a-f]{64}', result['sha256'])
    published = json.loads(rig.exec(rig.manager, ['/tooling/node', '/app/review.mjs', 'publish', subnet]))
    assert published['sha256'] == result['sha256'] and published['previous_sha256'] == expected
    rig.receipt['trusted_update_stage'] = dict(result, published_source_version=2, hostile_config_used=False)
    return result


def ready(rig, previous, log_offset, request_id):
    db = rig.sql("select status||'|'||version from app where app_name='agent-synthetic' and app_store_slug='migrated';")
    log = rig.out/'manager.sanitized.log'
    text = ''
    if log.exists():
        with log.open('rb') as stream:
            stream.seek(log_offset); raw = stream.read(64*2**20+1)
            assert len(raw) <= 64*2**20
            text = raw.decode(errors='replace')
    failure = any(marker in text for marker in (
        'Failed to update app '+URN+':', 'Failed to process update result for app '+URN+':',
        'Failed to restore app '+URN+' after an update error.', 'App '+URN+' update failed:'))
    # Running|1 is the shipped handler's terminal recovery status, not success.
    if failure or (db.split('|')[0] in ('', 'stopped', 'missing')) or db == 'running|1':
        diagnostic = {'action': 'update', 'request_id': request_id, 'database_status_version': db,
                      'synthetic_failure_log_seen': failure, 'read_only': True}
        rig.receipt['terminal_update_failure'] = diagnostic
        try:
            diagnostic['manager_http_status'] = rig.http('GET', '/api/apps/'+URN)[0]
            ids = rig.owned_fixture_ids()
            assert len(ids) <= 5 and all(re.fullmatch('[0-9a-f]{64}', cid) for cid in ids)
            diagnostic['owned_fixture_ids'] = ids
        finally:
            (rig.out/'terminal-update-failure.json').write_text(json.dumps(diagnostic, indent=2)+'\n')
        raise RuntimeError('terminal update failure; no retry')
    assert db in ('updating|1', 'updating|2', 'running|2'), 'unexpected update DB state'
    if db != 'running|2': return False
    # The attached log reader is asynchronous; do not confuse capture lag with
    # a successful launch on unsealed input. Once present, shape is fail-closed.
    up = [line for line in re.findall(r'Running docker compose with args ([^\n]+)', text)
          if ' up --detach --force-recreate --remove-orphans' in line]
    if not up: return False
    assert len(up) == 1 and 'user-config' not in up[0]
    assert re.search(r'-f /data/state/mandatory-seal-launches/launch-[A-Za-z0-9]+/effective\.json up ', up[0])
    ids = rig.owned_fixture_ids()
    return len(ids) == 5 and not previous.intersection(ids)


def transaction(rig, previous):
    """Exactly one PATCH, never retry ambiguous, rejected or terminal failure."""
    assert len(previous) == 5
    log = rig.out/'manager.sanitized.log'; offset = log.stat().st_size if log.exists() else 0
    status, body = rig.http('PATCH', '/api/app-lifecycle/'+URN+'/update', {'performBackup': False})
    assert 200 <= status < 300
    request_id = body['requestId']; assert str(uuid.UUID(request_id)) == request_id
    rig.receipt.setdefault('request_ids', []).append(request_id)
    rig.wait('terminal-update', lambda: ready(rig, set(previous), offset, request_id), 180)
    status, body = rig.http('GET', '/api/apps/'+URN)
    assert status == 200 and body['app']['status'] == 'running' and body['app']['version'] == 2
    assert rig.sql("select status||'|'||version from app where app_name='agent-synthetic' and app_store_slug='migrated';") == 'running|2'
    ids = rig.runtime(COMMAND_V2)
    assert len(set(ids)) == 5 and not set(ids).intersection(previous)
    # The ephemeral effective file is removed by the product in finally. Preserve
    # its fixed-path invocation proof, not fabricated bytes after its deletion.
    with log.open('rb') as stream:
        stream.seek(offset); text = stream.read(64*2**20+1).decode(errors='replace')
    assert len(text.encode()) <= 64*2**20+4
    launch = re.findall(r'Running docker compose with args ([^\n]+)', text)
    up = [line for line in launch if ' up --detach --force-recreate --remove-orphans' in line]
    assert len(up) == 1
    assert re.search(r'-f /data/state/mandatory-seal-launches/launch-[A-Za-z0-9]+/effective\.json up ', up[0])
    assert 'user-config' not in up[0]
    readback = json.loads(rig.exec(rig.manager, ['/tooling/node', '/app/review.mjs', 'readback']))
    assert readback['sha256'] == rig.receipt['trusted_update_stage']['sha256']
    assert readback['previous_sha256'] == rig.receipt['trusted_update_stage']['previous_sha256']
    assert readback['installed_version'] == 2 and readback['revision_command'] == COMMAND_V2
    rig.receipt['accepted_update'] = {'request_id': request_id, 'terminal_database': 'running|2',
        'actual_auth_http_readback': True, 'effective_snapshot_launch': True,
        'effective_snapshot_bytes_retained': False, 'runtime_ids': ids, 'seal_readback': readback}
    rig.receipt.setdefault('transactions', []).append({'action': 'update', 'method': 'PATCH',
        'request_id': request_id, 'terminal_database': 'running|2', 'actual_auth_http_readback': True})
    return ids
