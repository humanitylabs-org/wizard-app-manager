import fs from 'node:fs';
import path from 'node:path';
import { DATA_DIR } from '@/common/constants';
import type { ConfigurationService } from '@/core/config/configuration.service';
import type { AppStore } from '@/core/database/drizzle/types';
import { FilesystemService } from '@/core/filesystem/filesystem.service';
import type { LoggerService } from '@/core/logger/logger.service';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { WizardIndexStore } from '../wizard-index-store';
import { hermesRecipe } from '../../app-lifecycle/hermes-lock';

describe('owner-local exact Store selections', () => {
  let manager: WizardIndexStore;
  const folder = path.join(DATA_DIR, 'repos', 'wizardapps', 'apps', 'demo');
  const sha = 'a'.repeat(40);
  beforeEach(async () => {
    const logger = { debug: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() } as unknown as LoggerService;
    const configuration = {
      get: vi.fn().mockReturnValue({ dataDir: DATA_DIR }),
      getConfig: vi.fn().mockReturnValue({ directories: { dataDir: DATA_DIR } }),
    } as unknown as ConfigurationService;
    manager = new WizardIndexStore(configuration, new FilesystemService(logger), logger, {
      slug: 'wizardapps',
      url: 'https://index.test',
    } as AppStore);
    await fs.promises.mkdir(folder, { recursive: true });
    // A real selected image recipe carries its manifest too; source/build selections
    // deliberately proceed through upstream verification (covered in source-repair).
    await fs.promises.writeFile(path.join(folder, 'wizard.md'), '---\nid: demo\nruntime:\n  package: image\n---\n');
    await fs.promises.writeFile(path.join(folder, '.wizard-commit'), `${sha}\n`);
    await fs.promises.writeFile(path.join(folder, '.wizard-source'), JSON.stringify({ repo: 'org/demo' }));
    await fs.promises.writeFile(path.join(folder, '.wizard-selection'), JSON.stringify({ repo: 'org/demo', commit: sha }));
  });
  it('never consults or overwrites with the index even on a fresh copy lookup', async () => {
    const lookup = vi.spyOn(globalThis, 'fetch').mockRejectedValue(Error('must not contact index'));
    await manager.ensureApp('demo', true);
    expect(lookup).not.toHaveBeenCalled();
    expect(await fs.promises.readFile(path.join(folder, '.wizard-commit'), 'utf8')).toBe(`${sha}\n`);
    lookup.mockRestore();
  });
  it('rejects a corrupted selected pin rather than falling back', async () => {
    await fs.promises.writeFile(path.join(folder, '.wizard-selection'), JSON.stringify({ repo: 'foreign/demo', commit: sha }));
    await expect(manager.ensureApp('demo', true)).rejects.toThrow('invalid owner-local selection pin');
  });
  it('rejects a stale selected recipe commit', async () => {
    await fs.promises.writeFile(path.join(folder, '.wizard-commit'), 'b'.repeat(40));
    await expect(manager.ensureApp('demo', true)).rejects.toThrow('invalid owner-local selection pin');
  });
  it('refuses a selected Hermes recipe outside the shared lock', async () => {
    const hermes = path.join(DATA_DIR, 'repos', 'wizardapps', 'apps', 'hermes-agent');
    await fs.promises.mkdir(hermes, { recursive: true });
    await fs.promises.writeFile(path.join(hermes, '.wizard-commit'), sha);
    await fs.promises.writeFile(path.join(hermes, '.wizard-source'), JSON.stringify({ repo: hermesRecipe.repo }));
    await fs.promises.writeFile(path.join(hermes, '.wizard-selection'), JSON.stringify({ repo: hermesRecipe.repo, commit: sha }));
    await expect(manager.ensureApp('hermes-agent', true)).rejects.toThrow('shared exact lock');
  });
  it('selects the frozen Hermes commit even if the catalog offers a newer one', async () => {
    const target = manager as unknown as {
      lookup(name: string, fresh: boolean): Promise<{ repo: string; install: { git: string; commit: string } }>;
    };
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          apps: [
            {
              id: 'hermes-agent',
              repo: hermesRecipe.repo,
              install: { git: `https://github.com/${hermesRecipe.repo}`, commit: sha },
            },
          ],
        }),
        { status: 200 },
      ),
    );
    try {
      const rec = await target.lookup('hermes-agent', true);
      expect(rec.repo).toBe(hermesRecipe.repo);
      expect(rec.install.commit).toBe(hermesRecipe.commit);
      expect(rec.install.git).toBe(`https://github.com/${hermesRecipe.repo}`);
    } finally {
      fetch.mockRestore();
    }
  });
});
