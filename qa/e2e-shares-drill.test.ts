import { test, expect, spyOn } from 'bun:test';
import fs from 'node:fs/promises';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import * as yaml from 'yaml';
import { fixture, hash, sleepCompose, main } from './e2e-shares-drill';
import { WizardStorage, LEGACY_CLAUDE, COPY_IMAGE, targetOf } from '../packages/backend/src/modules/docker/wizard-storage';
import { BackupManager } from '../packages/backend/src/modules/backups/backup.manager';
import { ArchiveService } from '../packages/backend/src/core/archive/archive.service';
const exec = promisify(execFile);
const temp = () => fs.mkdtemp(path.join(process.env.TMPDIR!, 'shares-unit-'));

test('pinned real recipes compile, approval injects only provider bind, revoke removes it; no Docker', async () => {
  const root = await temp();
  try {
    const c = await fixture(root, true, 'unit-legacy');
    const before = yaml.parse(await c.store.prepare('hermes-agent:wizardapps', await c.compile('hermes-agent')));
    expect(before.services['hermes-agent'].volumes.some((v: any) => targetOf(v) === '/claude')).toBe(false);
    const proposal = (await c.store.proposed())[0]!;
    await c.store.change(proposal, true);
    const granted = await c.store.prepare('hermes-agent:wizardapps', await c.compile('hermes-agent'));
    const mount = yaml.parse(granted).services['hermes-agent'].volumes.find((v: any) => targetOf(v) === '/claude');
    expect(mount.source).toBe(`${root}/app-data/wizardapps/wizard-claude/data/claude`);
    const projected = yaml.parse(sleepCompose(granted, 'unit-prefix', 'hermes-agent'));
    expect(projected.services['hermes-agent'].volumes).toEqual(yaml.parse(granted).services['hermes-agent'].volumes);
    expect(projected.services['hermes-agent'].image).toBe(COPY_IMAGE);
    expect(projected.services['hermes-agent'].network_mode).toBe('none');
    expect(projected.services['hermes-agent'].ports).toBeUndefined();
    await c.store.change(proposal, false);
    const revoked = yaml.parse(await c.store.prepare('hermes-agent:wizardapps', await c.compile('hermes-agent')));
    expect(revoked.services['hermes-agent'].volumes.some((v: any) => targetOf(v) === '/claude')).toBe(false);
    expect(await c.store.proposed()).toEqual([]);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('injected legacy name controls real production upgrade lookup/copy, not recipe exception', async () => {
  const root = await temp();
  try {
    const c = await fixture(root, true, 'unused');
    const legacy = {
      services: {
        main: { image: COPY_IMAGE, user: `${process.getuid?.()}:${process.getgid?.()}`, volumes: ['login:/claude'], 'x-runtipi': { is_main: true } },
      },
      volumes: { login: { name: LEGACY_CLAUDE } },
    };
    for (const id of ['wizard-claude', 'hermes-agent']) {
      const dir = c.files.getAppPaths(`${id}:wizardapps` as any).appInstalledDir;
      await fs.writeFile(path.join(dir, 'wizard.md'), `---\nid: ${id}\n---\n`);
      await fs.writeFile(path.join(dir, 'docker-compose.yml'), yaml.stringify(legacy));
    }
    const calls: string[][] = [];
    // Unit test double only. Full rehearsal uses rejecting real execFile.
    const run = async (_file: string, args: string[]) => {
      calls.push(args);
      return { stdout: args[0] === 'volume' ? 'unit-disposable-legacy\n' : '' };
    };
    const store = new WizardStorage(c.dirs, run, 'unit-disposable-legacy');
    const result = yaml.parse(await store.prepare('hermes-agent:wizardapps', await c.compile('hermes-agent')));
    expect((await store.readGrants()).grants[0]?.approved).toBe('install');
    expect(result.services.main.volumes).toHaveLength(1);
    expect(result.services.main.volumes[0].source).toContain('/wizard-claude/data/claude');
    expect(calls.some((a) => a.includes('type=volume,src=unit-disposable-legacy,dst=/old,readonly'))).toBe(true);
    expect(calls.some((a) => a.some((v) => v.includes(`src=${LEGACY_CLAUDE},`)))).toBe(false);
    const grant = (await store.readGrants()).grants[0]!;
    await store.change(grant, false);
    expect(yaml.parse(await store.prepare('hermes-agent:wizardapps', await c.compile('hermes-agent'))).services.main.volumes).toEqual([]);
    expect(() => new WizardStorage(c.dirs, run, '../unsafe')).toThrow('Unsafe legacy');
    expect(new WizardStorage(c.dirs, run).legacyClaudeVolume).toBe(LEGACY_CLAUDE);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('production backup archives provider bytes only; production consumer data deletion preserves provider', async () => {
  const root = await temp();
  try {
    const c = await fixture(root, true, 'unit-legacy');
    const provider = c.files.getAppPaths('wizard-claude:wizardapps' as any).appDataDir;
    const login = path.join(provider, 'data/claude/.credentials.json');
    await fs.mkdir(path.dirname(login));
    const fake = 'UNIT-TEST-FAKE-LOGIN';
    await fs.writeFile(login, fake, { mode: 0o600 });
    const logger = { info() {}, debug() {} } as any;
    const backup = new BackupManager(new ArchiveService(logger), logger, c.config, c.filesystem, c.files);
    for (const id of ['wizard-claude', 'hermes-agent']) {
      await backup.backupApp(`${id}:wizardapps` as any);
      const dir = path.join(c.dirs.dataDir, 'backups/wizardapps', id);
      const archive = path.join(dir, (await fs.readdir(dir))[0]!);
      const listing = (await exec('tar', ['-tzf', archive])).stdout;
      if (id === 'wizard-claude') {
        expect(hash((await exec('tar', ['-xzOf', archive, './app-data/data/claude/.credentials.json'])).stdout)).toBe(hash(fake));
      } else expect(listing.includes('.credentials.json')).toBe(false);
    }
    await c.files.deleteAppDataDir('hermes-agent:wizardapps' as any);
    expect(await fs.readFile(login, 'utf8')).toBe(fake);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('dry-run compiles without privileged chown and cleans its planning tree', async () => {
  const root = await temp();
  const chown = spyOn(fs, 'chown').mockRejectedValue(new Error('Unprivileged host'));
  let printed = '';
  const output = spyOn(process.stdout, 'write').mockImplementation((text: any) => {
    printed += String(text);
    return true;
  });
  try {
    await main(['--dry-run', '--root', path.join(root, 'e2e-shares')]);
    expect(chown).not.toHaveBeenCalled();
    expect(printed).toContain('"hermes_mount_after_grant": true');
    expect(printed).toContain('production compiled compose');
    expect(await fs.readdir(path.join(root, 'e2e-shares'))).toEqual([]);
  } finally {
    chown.mockRestore();
    output.mockRestore();
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('full mode refuses without explicit opt-in/daemon before writing', async () => {
  const old = process.env.WIZARD_E2E_SHARES;
  delete process.env.WIZARD_E2E_SHARES;
  try {
    await expect(main(['--root', '/not-used/e2e-shares'])).rejects.toThrow('Set WIZARD_E2E_SHARES');
  } finally {
    if (old !== undefined) process.env.WIZARD_E2E_SHARES = old;
  }
});
