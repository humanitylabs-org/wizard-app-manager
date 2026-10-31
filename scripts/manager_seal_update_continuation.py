"""QA-only single trusted revision + one shipped update. No runtime on import."""
import hashlib
import json
from pathlib import Path
import re
import uuid

URN = 'agent-synthetic:migrated'
COMMAND_V1 = 'import time; time.sleep(1800)'
COMMAND_V2 = COMMAND_V1+' # trusted revision 2'


def assert_reuse_closure(reuse, prior_manifest, exceptions):
    """Count the actual non-QA baseline, never inherit a stale stage magic total."""
    assert all(path.startswith(('scripts/', 'docs/')) and not any(c in path for c in '*?[')
               for path in exceptions), 'QA exception scope widened'
    assert set(reuse['qa_exceptions']) == set(exceptions)
    expected = len(set(prior_manifest['files'])-set(exceptions))
    assert reuse['unchanged_export_files'] == expected, 'non-QA baseline count drift'
    assert reuse['full_non_qa_export_equal'] and reuse['backend_rebuilt'] is False
    return expected


def assert_qa_delta_inventory(actual, expected, exceptions):
    """Keep the historical baseline's exact QA delta inventory, not just totals."""
    assert actual == sorted(set(actual)) and actual == expected, 'explicit QA delta inventory drift'
    assert set(actual) <= set(exceptions), 'non-QA delta'
    return True


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
    assert re.fullmatch('[0-9a-f]{40}', published['upstream_commit'])
    assert published['publication']['schema_valid'] and published['publication']['tipi_version'] == 2
    rig.receipt['trusted_update_stage'] = dict(result, published_source_version=2, hostile_config_used=False,
        upstream_commit=published['upstream_commit'], publication=published['publication'])
    return result


def update_log(rig, log_offset):
    log = rig.out/'manager.sanitized.log'
    if not log.exists(): return ''
    with log.open('rb') as stream:
        stream.seek(log_offset); raw = stream.read(64*2**20+1)
    assert len(raw) <= 64*2**20
    return raw.decode(errors='replace')


def terminal_markers(text):
    failure = any(marker in text for marker in (
        'Failed to update app '+URN+':', 'Failed to process update result for app '+URN+':',
        'Failed to restore app '+URN+' after an update error.', 'App '+URN+' update failed:'))
    success = 'App '+URN+' update completed successfully' in text
    return success, failure


def diagnose(rig, request_id, db, text, reason):
    """Best-effort bounded read-only evidence; never retries the PATCH."""
    success, failure = terminal_markers(text)
    diagnostic = {'action':'update','request_id':request_id,'database_status_version':db,
        'synthetic_failure_log_seen':failure,'handler_success_log_seen':success,'reason':reason,
        'read_only':True,'correlation':'single PATCH; synthetic URN; post-request log offset (logs omit requestId)'}
    def capture(key, callback):
        try: diagnostic[key] = callback()
        except Exception as error: diagnostic[key] = {'diagnostic_error':type(error).__name__}
    capture('manager_http_status', lambda: rig.http('GET','/api/apps/'+URN)[0])
    def ids():
        value=rig.owned_fixture_ids()
        assert len(value)<=5 and all(re.fullmatch('[0-9a-f]{64}',cid) for cid in value)
        return value
    capture('owned_fixture_ids',ids)
    capture('database_row',lambda: rig.sql("select status||'|'||version||'|'||updated_at from app where app_name='agent-synthetic' and app_store_slug='migrated';")[:1024])
    def metadata():
        raw=rig.exec(rig.manager,['/tooling/node','/app/review.mjs','diagnostic'],timeout=20)
        assert len(raw.encode())<=16384
        return json.loads(raw)
    capture('synthetic_metadata',metadata)
    info=diagnostic.get('synthetic_metadata',{}).get('installed',{})
    diagnostic['finding'] = ('product-version-write-defect' if success and db=='running|1'
        and info.get('schema_valid') and info.get('available') and info.get('tipi_version')==2
        else 'stale-installed-metadata' if success and db=='running|1' and info.get('tipi_version')==1
        else 'unclassified-see-evidence')
    def tail():
        log=rig.out/'manager.sanitized.log'
        with log.open('rb') as stream:
            stream.seek(max(0,log.stat().st_size-131072)); raw=stream.read(131072)
        return '\n'.join(raw.decode(errors='replace').splitlines()[-200:])[-65536:]
    capture('manager_log_last_200_lines',tail)
    raw=json.dumps(diagnostic,indent=2)+'\n'
    assert len(raw.encode())<=128*1024
    rig.receipt['terminal_update_failure']=diagnostic
    (rig.out/'terminal-update-failure.json').write_text(raw)
    return diagnostic


def ready(rig, previous, log_offset, request_id):
    db = rig.sql("select status||'|'||version from app where app_name='agent-synthetic' and app_store_slug='migrated';")
    text = update_log(rig,log_offset)
    success, failure = terminal_markers(text)
    if success or failure:
        # The first DB query can precede the success log; sample again after it.
        db = rig.sql("select status||'|'||version from app where app_name='agent-synthetic' and app_store_slug='migrated';")
    # Polls may observe the restored old version/status while the handler is
    # still running or its attached log capture lags. Only a terminal handler
    # marker in this one-request window makes running|1 a terminal failure.
    if failure or (success and db != 'running|2'):
        diagnose(rig,request_id,db,text,'handler-failure' if failure else 'success-with-wrong-db-version')
        raise RuntimeError('terminal update failure; no retry')
    if db not in ('updating|1','updating|2','running|1','running|2','stopped|1','stopped|2','missing|1',''):
        diagnose(rig,request_id,db,text,'unexpected-db-state')
        raise RuntimeError('unexpected update DB state; no retry')
    if not success or db != 'running|2': return False
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
    try:
        rig.wait('terminal-update', lambda: ready(rig, set(previous), offset, request_id), 180)
    except Exception:
        if 'terminal_update_failure' not in rig.receipt:
            try: db=rig.sql("select status||'|'||version from app where app_name='agent-synthetic' and app_store_slug='migrated';")
            except Exception: db='unreadable'
            diagnose(rig,request_id,db,update_log(rig,offset),'bounded-wait-or-observer-failure')
        raise
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
    assert readback['installed_config_sha256'] == rig.receipt['trusted_update_stage']['publication']['sha256']
    rig.receipt['accepted_update'] = {'request_id': request_id, 'terminal_database': 'running|2',
        'actual_auth_http_readback': True, 'effective_snapshot_launch': True,
        'effective_snapshot_bytes_retained': False, 'runtime_ids': ids, 'seal_readback': readback}
    rig.receipt.setdefault('transactions', []).append({'action': 'update', 'method': 'PATCH',
        'request_id': request_id, 'terminal_database': 'running|2', 'actual_auth_http_readback': True})
    return ids
