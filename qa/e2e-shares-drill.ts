#!/usr/bin/env bun
/** Disposable decisions #79/#80 integration rehearsal. See e2e-shares-drill.md.
 * No real credentials, owner recipes, ports, host accounts or live containers.
 * Run via bun --tsconfig-override ./qa/tsconfig.e2e-shares.json.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import * as yaml from 'yaml';
import { WizardStorage, COPY_IMAGE, LEGACY_CLAUDE, targetOf } from '../packages/backend/src/modules/docker/wizard-storage';
import { DockerComposeBuilder } from '../packages/backend/src/modules/docker/builders/compose.builder';
import { BackupManager } from '../packages/backend/src/modules/backups/backup.manager';
import { AppFilesManager } from '../packages/backend/src/modules/apps/app-files-manager';
import { FilesystemService } from '../packages/backend/src/core/filesystem/filesystem.service';
import { ArchiveService } from '../packages/backend/src/core/archive/archive.service';

const exec = promisify(execFile);
const repo = path.resolve(import.meta.dir, '..');
const ids = ['wizard-claude', 'hermes-agent'] as const;
type Id = (typeof ids)[number];
const urn = (id: Id) => `${id}:wizardapps` as any;
export const hash = (s: string | Buffer) => createHash('sha256').update(s).digest('hex');
const FAKE = '{"drill":"FAKE-NOT-A-LOGIN","accessToken":"not-a-token"}\n';
const logger = {
  info() {
    /* Quiet harness logger. */
  },
  debug() {
    /* Quiet harness logger. */
  },
  warn() {
    /* Quiet harness logger. */
  },
  error(...args: unknown[]) {
    throw new Error(args.join(' '));
  },
} as any;
const PLAN = [
  'Create only unique labelled throwaway volumes/containers; never use the live fixed-name volume.',
  'Install hash-pinned companion recipes and compile via DockerComposeBuilder.',
  'Propose/approve with WizardStorage; migrate fake legacy login with production migrate; prove copy-once/source retention.',
  'Audit numeric ownership/modes on a Linux-side scratch volume (macOS virtiofs remaps ownership).',
  'Start compiled mounts with pinned Alpine sleep, network none, no ports; compare hashes, write both ways and contend on mkdir lock.',
  'Run BackupManager + FilesystemService + ArchiveService for provider and consumer; inspect real tar bytes.',
  'Revoke via WizardStorage, force-recreate consumer, inspect absence; delete consumer via AppFilesManager.deleteAppDataDir.',
  'Exercise old fixed-name recipe pair and install auto-approval via injected disposable legacy volume; denial must survive.',
  'Finally remove owned Docker objects and temp state; write receipt.json with hashes/check booleans and verify zero leftovers.',
];

/** Only adapt the production filesystem's fixed appliance roots to this isolated root.
 * Copy, archive, uninstall/data-removal implementations are production code.
 */
class DrillFilesystem extends FilesystemService {
  constructor(readonly root: string) {
    super(logger);
  }
  override getSafeFilePath(p: string) {
    const full = path.resolve(p);
    assert(full === this.root || full.startsWith(`${this.root}${path.sep}`), `Path outside drill: ${p}`);
    return full;
  }
  override async createTempDirectory(_prefix: string) {
    const temp = path.join(this.root, 'tmp');
    await fs.mkdir(temp, { recursive: true });
    return fs.mkdtemp(path.join(temp, 'backup-'));
  }
}
/** Planning only: do not run or fake a Docker migration in --dry-run. */
class PlanningStorage extends WizardStorage {
  override async migrate() {
    /* Planning skips Docker; never claim a real migration. */
  }
}

export async function fixture(root: string, dry: boolean, legacyVolume: string) {
  const dirs = { dataDir: path.join(root, 'manager'), appDataDir: path.join(root, 'app-data') };
  const provenance = JSON.parse(await fs.readFile(path.join(repo, 'qa/fixtures/e2e-shares/provenance.json'), 'utf8'));
  for (const row of provenance) {
    const bytes = await fs.readFile(path.join(repo, row.file));
    assert.equal(hash(bytes), row.sha256, `Fixture drift: ${row.file}`);
    const id: Id = row.repo === 'claude-connection' ? 'wizard-claude' : 'hermes-agent';
    const dest = path.join(dirs.dataDir, 'apps/wizardapps', id, path.basename(row.source));
    await fs.mkdir(path.dirname(dest), { recursive: true });
    await fs.writeFile(dest, bytes);
  }
  const run = async (file: string, args: string[]) => {
    // Keep production migration helpers in the same cleanup namespace, including
    // interrupted CLI calls; never change their mounts or copy command.
    const actual =
      args[0] === 'run'
        ? ['run', '--name', `${legacyVolume}-migration-${randomUUID().slice(0, 8)}`, '--label', `wizard.e2e-shares=${legacyVolume}`, ...args.slice(1)]
        : args;
    return exec(file, actual, { timeout: 120_000, maxBuffer: 4 * 1024 * 1024 });
  };
  const store = dry ? new PlanningStorage(dirs, undefined, legacyVolume) : new WizardStorage(dirs, run, legacyVolume);
  // Never read/update the real owner's registry, even when inherited from a worker.
  process.env.WIZARD_OS_HOME = path.join(dirs.dataDir, 'state/wizard-os');
  const config = {
    get: (key: string) => {
      assert.equal(key, 'directories');
      return dirs;
    },
    getConfig: () => ({ directories: dirs }),
  } as any;
  const filesystem = new DrillFilesystem(root);
  const files = new AppFilesManager(config, filesystem, logger);
  for (const id of ids) {
    const data = files.getAppPaths(urn(id)).appDataDir;
    await fs.mkdir(path.join(data, 'data'), { recursive: true });
    await fs.writeFile(path.join(data, 'app.env'), `APP_DATA_DIR=${data}\n`);
  }
  const compile = async (id: Id) => {
    const source = yaml.parse(await fs.readFile(path.join(files.getAppPaths(urn(id)).appInstalledDir, 'docker-compose.yml'), 'utf8'));
    const built = new DockerComposeBuilder().getDockerCompose(source, {}, urn(id), '10.253.252.0/24', 'arm64');
    const text = built.replaceAll('${APP_DATA_DIR}', files.getAppPaths(urn(id)).appDataDir);
    const doc = yaml.parse(text);
    assert.equal(doc.volumes, undefined);
    return text;
  };
  return { dirs, store, files, filesystem, config, compile, provenance };
}

/** Strip only execution machinery, never alter a compiled mount. */
export function sleepCompose(text: string, prefix: string, id: Id) {
  const input = yaml.parse(text);
  const services: Record<string, any> = {};
  for (const [name, svc] of Object.entries(input.services) as [string, any][]) {
    services[name] = {
      image: COPY_IMAGE,
      command: ['sleep', '600'],
      restart: 'no',
      network_mode: 'none',
      container_name: `${prefix}-${id}-${name}`,
      user: svc.user ?? '0:0',
      read_only: true,
      cap_drop: ['ALL'],
      cap_add: ['DAC_OVERRIDE'],
      security_opt: ['no-new-privileges:true'],
      // Root exec probes need DAC_OVERRIDE on macOS virtiofs; see runtime note.
      volumes: structuredClone(svc.volumes ?? []),
      labels: { 'wizard.e2e-shares': prefix },
    };
    assert.deepEqual(services[name].volumes, svc.volumes ?? []);
    for (const mount of services[name].volumes) {
      assert(
        typeof mount === 'string' ? mount.split(':')[0]?.startsWith('/') : mount.type === 'bind' && path.isAbsolute(mount.source),
        'Only absolute bind mounts allowed',
      );
    }
  }
  return yaml.stringify({ services });
}

export async function main(argv = process.argv.slice(2)) {
  const dry = argv.includes('--dry-run');
  const args = argv.filter((a) => a !== '--dry-run');
  assert(args.length === 2 && args[0] === '--root', 'Usage: ... qa/e2e-shares-drill.ts [--dry-run] --root <isolated-e2e-shares-root>');
  const base = path.resolve(args[1]!);
  assert(path.basename(base) === 'e2e-shares', 'Root must be a dedicated e2e-shares directory');
  if (!dry) assert(process.env.WIZARD_E2E_SHARES === '1' && !!process.env.DOCKER_HOST, 'Set WIZARD_E2E_SHARES=1 and explicit DOCKER_HOST');
  await fs.mkdir(base, { recursive: true });
  assert.equal(await fs.realpath(base), base, 'Symlinked roots refused');
  const prefix = `wz-shares-${Date.now()}-${randomUUID().slice(0, 8)}`;
  const root = path.join(base, prefix);
  await fs.mkdir(root);
  const volume = `${prefix}-legacy`;
  const auditVolume = `${prefix}-unix-audit`;
  const checks: Record<string, boolean> = {};
  const hashes: Record<string, string> = { source: hash(FAKE) };
  const docker = async (args: string[]) => (await exec('docker', args, { maxBuffer: 4 * 1024 * 1024, timeout: 120_000 })).stdout;
  const volumesOwned: string[] = [];
  const helper = async (mounts: string[], script: string) => {
    const name = `${prefix}-helper-${randomUUID().slice(0, 8)}`;
    return await docker([
      'run',
      '--rm',
      '--name',
      name,
      '--label',
      `wizard.e2e-shares=${prefix}`,
      '--network',
      'none',
      ...mounts.flatMap((m) => ['--mount', m]),
      COPY_IMAGE,
      'sh',
      '-ec',
      script,
    ]);
  };
  const check = (name: string, ok: boolean) => {
    checks[name] = ok;
    assert(ok, name);
  };
  let error: string | null = null;
  let context: Awaited<ReturnType<typeof fixture>> | undefined;
  const composeFiles: string[] = [];
  try {
    context = await fixture(root, dry, volume);
    const { store, compile, files, config, filesystem } = context;
    const before = await store.prepare(urn('hermes-agent'), await compile('hermes-agent'));
    check(
      'hermes_no_mount_before_grant',
      !Object.values(yaml.parse(before).services).some((s: any) => s.volumes?.some((v: any) => targetOf(v) === '/claude')),
    );
    const proposal = (await store.proposed('hermes-agent'))[0]!;
    check('production_proposal', proposal?.share === 'claude-login');
    if (!dry) {
      // All operational legacy lookups select this disposable name; the literal
      // LEGACY_CLAUDE recipe name still goes through the production rewrite path.
      await docker(['volume', 'create', '--label', `wizard.e2e-shares=${prefix}`, volume]);
      volumesOwned.push(volume);
      const sourceMount = `type=volume,src=${volume},dst=/old`;
      await helper(
        [sourceMount],
        `mkdir -p /old/nested /old/.oauth_refresh.lock; printf '%s' '${FAKE}' > /old/.credentials.json; printf '%s' '{"drill":true}' > /old/.claude.json; printf nested > /old/nested/proof; chown -R 1000:1000 /old; chmod 600 /old/.credentials.json; chmod 700 /old/nested`,
      );
      const source = await helper([sourceMount], 'sha256sum /old/.credentials.json');
      check('legacy_source_hash', source.split(/\s/)[0] === hashes.source);
    }
    const providerData = files.getAppPaths(urn('wizard-claude')).appDataDir;
    // Planning does not run the privileged copy helper that creates this folder
    // in the full rehearsal. Precreate it without pretending to chown on macOS.
    if (dry) await fs.mkdir(path.join(providerData, 'data/claude'), { recursive: true });
    const provider = await store.prepare(urn('wizard-claude'), await compile('wizard-claude'));
    check('claude_owned_bind', yaml.parse(provider).services['wizard-claude-engine'].volumes.includes(`${providerData}/data/claude:/claude`));
    await store.change(proposal, true);
    check(
      'approval_readback',
      (await store.readGrants()).grants.some((g) => g.kind === 'share' && g.approved === 'owner'),
    );
    const after = await store.prepare(urn('hermes-agent'), await compile('hermes-agent'));
    check(
      'hermes_mount_after_grant',
      yaml.parse(after).services['hermes-agent'].volumes.some((v: any) => v.target === '/claude' && v.source === `${providerData}/data/claude`),
    );
    if (dry) {
      process.stdout.write(
        `${JSON.stringify({ mode: 'DRY-RUN: migration/start/backup/uninstall NOT executed', prefix, image: COPY_IMAGE, legacy_volume: volume, expected_runtime: '2-5 minutes warm; up to 10 minutes cold image pull', plan: PLAN, compile_checks: checks, provenance: context.provenance }, null, 2)}\n`,
      );
      for (const [label, text] of [
        ['claude', provider],
        ['hermes-before-grant', before],
        ['hermes-after-grant', after],
      ]) {
        process.stdout.write(`--- ${label} (production compiled compose; DO NOT start directly) ---\n${text}\n`);
        process.stdout.write(
          `--- ${label} (safe runtime projection; mounts unchanged) ---\n${sleepCompose(text!, prefix, label === 'claude' ? 'wizard-claude' : 'hermes-agent')}\n`,
        );
      }
      return;
    }
    const apps = await store.apps();
    const p = apps.find((a) => a.manifest.id === 'wizard-claude')!;
    const login = path.join(providerData, 'data/claude/.credentials.json');
    check('host_migrated_hash', hash(await fs.readFile(login)) === hashes.source);
    const marker = path.join(providerData, 'data/.claude.wizard-migrated');
    const markBefore = await fs.stat(marker);
    await fs.writeFile(path.join(providerData, 'data/claude/copy-once'), 'keep');
    await store.migrate(volume, 'data/claude', p);
    check(
      'migration_noop',
      (await fs.stat(marker)).mtimeMs === markBefore.mtimeMs &&
        (await fs.readFile(path.join(providerData, 'data/claude/copy-once'), 'utf8')) === 'keep',
    );
    check('legacy_retained', (await docker(['volume', 'inspect', volume, '--format', '{{.Name}}'])).trim() === volume);
    // Numeric Unix audit: a separate scratch Docker volume avoids macOS virtiofs
    // uid remapping. Invoke the SAME production migrate(), not a copied cp script.
    await docker(['volume', 'create', '--label', `wizard.e2e-shares=${prefix}`, auditVolume]);
    volumesOwned.push(auditVolume);
    const auditHost = (await docker(['volume', 'inspect', auditVolume, '--format', '{{.Mountpoint}}'])).trim();
    await helper([`type=volume,src=${auditVolume},dst=/audit`], 'mkdir /audit/data');
    await store.migrate(volume, 'data/claude', { ...p, data: path.join(root, 'unix-audit-manager'), hostData: auditHost });
    const attrs = await helper(
      [`type=volume,src=${auditVolume},dst=/audit`],
      'stat -c "%u:%g:%a" /audit/data/claude/.credentials.json /audit/data/claude/nested; cat /audit/data/claude/nested/proof; test -d /audit/data/claude/.oauth_refresh.lock; test -f /audit/data/.claude.wizard-migrated',
    );
    check('unix_uid_gid_mode', attrs.startsWith('1000:1000:600\n1000:1000:700\nnested'));
    check('dotfiles_nested_lock_marker', attrs.endsWith('nested'));
    const runtime = new Map<Id, string>();
    for (const [id, text] of [
      ['wizard-claude', provider],
      ['hermes-agent', after],
    ] as [Id, string][]) {
      const file = path.join(root, `${id}.runtime.yml`);
      await fs.writeFile(file, sleepCompose(text, prefix, id));
      composeFiles.push(file);
      runtime.set(id, file);
    }
    const up = async (id: Id) => docker(['compose', '-p', `${prefix}-${id}`, '-f', runtime.get(id)!, 'up', '-d', '--force-recreate', '--no-build']);
    // Creating containers under prepare's grant lock exercises the real boundary.
    await store.prepare(urn('wizard-claude'), await compile('wizard-claude'), async () => {
      await up('wizard-claude');
    });
    await store.prepare(urn('hermes-agent'), await compile('hermes-agent'), async () => {
      await up('hermes-agent');
    });
    const container = (id: Id) => `${prefix}-${id}-${id === 'wizard-claude' ? 'wizard-claude-engine' : 'hermes-agent'}`;
    // Probe with root + default DAC capability, because virtiofs remaps owners.
    // Containers keep compiled user; this is mount semantics, not an app login test.
    const probe = (id: Id, script: string) => docker(['exec', '--user', '0:0', container(id), 'sh', '-ec', script]);
    hashes.hermes = (await probe('hermes-agent', 'sha256sum /claude/.credentials.json')).split(/\s/)[0]!;
    check('hermes_reads_source', hashes.hermes === hashes.source);
    await probe('hermes-agent', 'printf hermes-write > /claude/from-hermes');
    check(
      'hermes_write_visible_claude_host',
      (await probe('wizard-claude', 'cat /claude/from-hermes')) === 'hermes-write' &&
        (await fs.readFile(path.join(providerData, 'data/claude/from-hermes'), 'utf8')) === 'hermes-write',
    );
    await probe('wizard-claude', 'printf claude-write > /claude/from-claude');
    check(
      'claude_write_visible_hermes_host',
      (await probe('hermes-agent', 'cat /claude/from-claude')) === 'claude-write' &&
        (await fs.readFile(path.join(providerData, 'data/claude/from-claude'), 'utf8')) === 'claude-write',
    );
    await probe('wizard-claude', 'rmdir /claude/.oauth_refresh.lock; mkdir /claude/.oauth_refresh.lock');
    // Nonzero is expected ONLY for EEXIST; prove the directory still exists.
    const contended = await probe(
      'hermes-agent',
      'if mkdir /claude/.oauth_refresh.lock 2>/dev/null; then rmdir /claude/.oauth_refresh.lock; exit 44; else test -d /claude/.oauth_refresh.lock; printf contended; fi',
    );
    await probe('wizard-claude', 'rmdir /claude/.oauth_refresh.lock');
    await probe('hermes-agent', 'mkdir /claude/.oauth_refresh.lock; rmdir /claude/.oauth_refresh.lock');
    check('mkdir_lock_contention_and_release', contended === 'contended');
    // Stop writers before snapshotting; leave containers for actual revoke proof.
    for (const id of ids) await docker(['compose', '-p', `${prefix}-${id}`, '-f', runtime.get(id)!, 'stop', '-t', '1']);
    const backup = new BackupManager(new ArchiveService(logger), logger, config, filesystem, files);
    for (const id of ids) {
      await backup.backupApp(urn(id));
      const dir = path.join(context.dirs.dataDir, 'backups/wizardapps', id);
      const names = await fs.readdir(dir);
      assert.equal(names.length, 1);
      const archive = path.join(dir, names[0]!);
      const listing = (await exec('tar', ['-tzf', archive])).stdout;
      if (id === 'wizard-claude') {
        const bytes = (await exec('tar', ['-xzOf', archive, './app-data/data/claude/.credentials.json'])).stdout;
        hashes.backup = hash(bytes);
        check('provider_backup_login_bytes', hashes.backup === hashes.source);
      } else {
        check('consumer_backup_excludes_login', !listing.includes('.credentials.json') && !listing.includes('data/claude'));
      }
    }
    await store.change(proposal, false);
    check('revocation_readback', !(await store.readGrants()).grants.some((g) => g.kind === 'share'));
    const revoked = await store.prepare(urn('hermes-agent'), await compile('hermes-agent'), async (text) => {
      await fs.writeFile(runtime.get('hermes-agent')!, sleepCompose(text, prefix, 'hermes-agent'));
      await up('hermes-agent');
    });
    check('revoke_compiled_mount_gone', !yaml.parse(revoked).services['hermes-agent'].volumes.some((v: any) => targetOf(v) === '/claude'));
    const inspected = JSON.parse(await docker(['inspect', container('hermes-agent')]));
    check('revoke_running_mount_gone', !inspected[0].Mounts.some((m: any) => m.Destination === '/claude'));
    await docker(['compose', '-p', `${prefix}-hermes-agent`, '-f', runtime.get('hermes-agent')!, 'down', '-t', '1']);
    const snapshot = async () => {
      const names = (await fs.readdir(providerData, { recursive: true })).sort();
      const entries = await Promise.all(
        names.map(async (name) => {
          const full = path.join(providerData, name);
          const stat = await fs.lstat(full);
          return [name, stat.mode, stat.isFile() ? hash(await fs.readFile(full)) : 'directory'];
        }),
      );
      return hash(JSON.stringify(entries));
    };
    const providerBeforeUninstall = await snapshot();
    await store.remove('hermes-agent');
    await files.deleteAppDataDir(urn('hermes-agent'));
    hashes.provider_before_uninstall = providerBeforeUninstall;
    hashes.provider_after_uninstall = await snapshot();
    check('provider_tree_unchanged', hashes.provider_after_uninstall === hashes.provider_before_uninstall);
    check(
      'consumer_uninstall_preserves_provider',
      hash(await fs.readFile(login)) === hashes.source &&
        !(await fs.stat(files.getAppPaths(urn('hermes-agent')).appDataDir).then(
          () => true,
          () => false,
        )),
    );
    // Old fixed-name pair: production compatibility shim + rewrite + install grant.
    // This is a separate manager tree, never modifies pinned modern fixtures.
    const legacy = await fixture(path.join(root, 'legacy-upgrade'), false, volume);
    for (const id of ids) {
      const dir = legacy.files.getAppPaths(urn(id)).appInstalledDir;
      const doc = {
        services: { main: { image: COPY_IMAGE, user: '0:0', volumes: ['login:/claude'], 'x-runtipi': { is_main: true } } },
        volumes: { login: { name: LEGACY_CLAUDE } },
      };
      await fs.writeFile(path.join(dir, 'wizard.md'), `---\nid: ${id}\n---\n`);
      await fs.writeFile(path.join(dir, 'docker-compose.yml'), yaml.stringify(doc));
    }
    await legacy.store.prepare(urn('wizard-claude'), await legacy.compile('wizard-claude'));
    const inherited = await legacy.store.readGrants();
    check(
      'legacy_install_autoapprove',
      inherited.claude_share_upgrade_v1 === true && inherited.grants.some((g) => g.kind === 'share' && g.approved === 'install'),
    );
    const oldHermes = yaml.parse(await legacy.store.prepare(urn('hermes-agent'), await legacy.compile('hermes-agent')));
    check(
      'legacy_consumer_provider_only',
      oldHermes.services.main.volumes.length === 1 && oldHermes.services.main.volumes[0].source.includes('/wizard-claude/data/claude'),
    );
    await legacy.store.change(proposal, false);
    const denied = yaml.parse(await legacy.store.prepare(urn('hermes-agent'), await legacy.compile('hermes-agent')));
    check('legacy_denial_not_reapproved', !denied.services.main.volumes.some((v: any) => targetOf(v) === '/claude'));
  } catch (e) {
    error = e instanceof Error ? e.message : String(e);
  } finally {
    const failures: string[] = [];
    if (!dry) {
      // Only remove exact prefixed objects; migration helpers also carry this
      // namespace, so interrupted docker run calls cannot leave unowned helpers.
      for (const file of composeFiles) {
        const id = path.basename(file).startsWith('wizard-claude') ? 'wizard-claude' : 'hermes-agent';
        await docker(['compose', '-p', `${prefix}-${id}`, '-f', file, 'down', '-t', '1']).catch((e) => failures.push(String(e)));
      }
      const containers = (
        await docker(['ps', '-a', '--format', '{{.Names}}']).catch((e) => {
          failures.push(String(e));
          return '';
        })
      )
        .split(/\r?\n/)
        .filter((n) => n.startsWith(`${prefix}-`));
      for (const name of containers) await docker(['rm', '-f', name]).catch((e) => failures.push(String(e)));
      const createdVolumes = (
        await docker(['volume', 'ls', '--format', '{{.Name}}']).catch((e) => {
          failures.push(String(e));
          return '';
        })
      )
        .split(/\r?\n/)
        .filter((n) => n.startsWith(`${prefix}-`));
      for (const name of new Set([...volumesOwned, ...createdVolumes])) await docker(['volume', 'rm', name]).catch((e) => failures.push(String(e)));
      const remainingContainers = (
        await docker(['ps', '-a', '--format', '{{.Names}}']).catch((e) => {
          failures.push(String(e));
          return prefix;
        })
      )
        .split(/\r?\n/)
        .filter((n) => n.startsWith(prefix));
      const remainingVolumes = (
        await docker(['volume', 'ls', '--format', '{{.Name}}']).catch((e) => {
          failures.push(String(e));
          return prefix;
        })
      )
        .split(/\r?\n/)
        .filter((n) => n.startsWith(prefix));
      checks.cleanup_no_containers = remainingContainers.length === 0;
      checks.cleanup_no_volumes = remainingVolumes.length === 0;
    }
    // Retain receipt + rendered composes only, never fake credentials or archives.
    for (const entry of await fs.readdir(root)) {
      if (!entry.endsWith('.runtime.yml'))
        await fs.rm(path.join(root, entry), { recursive: true, force: true }).catch((e) => failures.push(String(e)));
    }
    if (dry) {
      await fs.rm(root, { recursive: true, force: true });
    } else {
      if (failures.length) error = [error, ...failures].filter(Boolean).join('; ');
      const receipt = {
        schema: 1,
        prefix,
        passed: !error && Object.values(checks).every(Boolean),
        error,
        checks,
        hashes,
        image: COPY_IMAGE,
        legacy_volume: volume,
        real_fixed_volume_touched: false,
        runtime_probe_uid: '0:0 (virtiofs ownership remap; numeric preservation audited separately on Linux)',
        source_revision: process.env.WIZARD_DRILL_REVISION ?? null,
        expected_runtime: '2-5 minutes warm; up to 10 cold',
        cleanup_errors: failures,
      };
      await fs.writeFile(path.join(root, 'receipt.json'), `${JSON.stringify(receipt, null, 2)}\n`);
      process.stdout.write(`${JSON.stringify({ receipt: path.join(root, 'receipt.json'), ...receipt })}\n`);
      if (!receipt.passed) error ??= 'One or more checks failed';
    }
  }
  if (error) throw new Error(error);
}
if (import.meta.main) await main();
