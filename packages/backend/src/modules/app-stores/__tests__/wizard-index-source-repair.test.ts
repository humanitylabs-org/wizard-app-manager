import fs from 'node:fs';
import path from 'node:path';
import { DATA_DIR } from '@/common/constants';
import type { ConfigurationService } from '@/core/config/configuration.service';
import type { AppStore } from '@/core/database/drizzle/types';
import { FilesystemService } from '@/core/filesystem/filesystem.service';
import type { LoggerService } from '@/core/logger/logger.service';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { sourceDir } from '../packaging';
import { WizardIndexStore } from '../wizard-index-store';

// An app prepared at the index's commit whose source copy (source/build packaging) went missing used to stay
// "ready": its compose then mounted an empty ${APP_SOURCE_DIR} and every update failed with
// "invalid spec: :/app:ro: empty section between colons". The same commit must be re-prepared instead.
describe('re-prepare a missing source copy at the same commit', () => {
  const appData = path.join(DATA_DIR, 'app-data-src-test');
  const folder = path.join(DATA_DIR, 'repos', 'wizardapps', 'apps', 'srcapp');
  const sha = 'c'.repeat(40);
  let manager: WizardIndexStore;
  let fetched: string[];

  const wizardMd = (pkg: string) => `---\nid: srcapp\nruntime:\n  config: c.json\n  compose: d.yml\n  package: ${pkg}\n---\n`;

  beforeEach(async () => {
    const logger = { debug: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() } as unknown as LoggerService;
    const configuration = {
      get: vi.fn().mockReturnValue({ dataDir: DATA_DIR, appDataDir: appData }),
      getConfig: vi.fn().mockReturnValue({ directories: { dataDir: DATA_DIR, appDataDir: appData } }),
    } as unknown as ConfigurationService;
    manager = new WizardIndexStore(configuration, new FilesystemService(logger), logger, {
      slug: 'wizardapps',
      url: 'https://index.test',
    } as AppStore);
    await fs.promises.rm(folder, { recursive: true, force: true });
    await fs.promises.rm(appData, { recursive: true, force: true });
    await fs.promises.mkdir(folder, { recursive: true });
    await fs.promises.writeFile(path.join(folder, '.wizard-commit'), `${sha}\n`);
    fetched = [];
    const target = manager as unknown as {
      lookup(): Promise<unknown>;
      fetchCommit(url: string, commit: string, dest: string): Promise<void>;
    };
    target.lookup = async () => ({ id: 'srcapp', repo: 'org/srcapp', install: { git: 'https://github.com/org/srcapp', commit: sha } });
    // Stop right after the decision to fetch: the rest of materialize is covered elsewhere.
    target.fetchCommit = async (_u, commit) => {
      fetched.push(commit);
      throw new Error('fetched');
    };
  });

  it('refetches a source package whose copy is gone', async () => {
    await fs.promises.writeFile(path.join(folder, 'wizard.md'), wizardMd('source'));
    await expect(manager.ensureApp('srcapp', true)).rejects.toThrow('fetched');
    expect(fetched).toEqual([sha]);
  });

  it('leaves a source package alone when its copy exists', async () => {
    await fs.promises.writeFile(path.join(folder, 'wizard.md'), wizardMd('source'));
    await fs.promises.mkdir(sourceDir(appData, 'wizardapps', 'srcapp', sha), { recursive: true });
    await manager.ensureApp('srcapp', true);
    expect(fetched).toEqual([]);
  });

  it('leaves image packages alone (no source copy needed)', async () => {
    await fs.promises.writeFile(path.join(folder, 'wizard.md'), wizardMd('image'));
    await manager.ensureApp('srcapp', true);
    expect(fetched).toEqual([]);
  });
});
