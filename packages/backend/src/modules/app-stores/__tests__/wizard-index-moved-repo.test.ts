import fs from 'node:fs';
import path from 'node:path';
import { DATA_DIR } from '@/common/constants';
import type { ConfigurationService } from '@/core/config/configuration.service';
import type { AppStore } from '@/core/database/drizzle/types';
import { FilesystemService } from '@/core/filesystem/filesystem.service';
import type { LoggerService } from '@/core/logger/logger.service';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WizardIndexStore } from '../wizard-index-store';

// wizard-app-manager#829: oscarhenrycollins/photos moved to humanitylabs-org/photos but the index still lists both
// under id photos, old one first. The copy must follow the repo the box recorded, not the index order.
const OLD = 'oscarhenrycollins/photos';
const NEW = 'humanitylabs-org/photos';
const rec = (repo: string, c: string) => ({ id: 'photos', repo, install: { git: `https://github.com/${repo}`, commit: c.repeat(40) } });

describe('an id listed by two repos', () => {
  let manager: WizardIndexStore;
  let target: { lookup(name: string, fresh: boolean): Promise<{ repo: string } | null> };
  const folder = path.join(DATA_DIR, 'repos', 'wizardapps', 'apps', 'photos');
  let fetch: ReturnType<typeof vi.spyOn>;
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
    target = manager as unknown as typeof target;
    await fs.promises.mkdir(folder, { recursive: true });
    fetch = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation(async () => new Response(JSON.stringify({ apps: [rec(OLD, 'a'), rec(NEW, 'b')] }), { status: 200 }));
  });
  afterEach(() => fetch.mockRestore());

  it('follows the repo the box recorded after the move', async () => {
    await fs.promises.writeFile(path.join(folder, '.wizard-source'), JSON.stringify({ repo: NEW, source: 'standard' }));
    expect((await target.lookup('photos', true))?.repo).toBe(NEW);
  });
  it('keeps the recorded repo before the move (never switches by index order)', async () => {
    await fs.promises.writeFile(path.join(folder, '.wizard-source'), JSON.stringify({ repo: OLD, source: 'standard' }));
    expect((await target.lookup('photos', true))?.repo).toBe(OLD);
  });
  it('uses the first hit when nothing is recorded or the recorded repo is gone', async () => {
    await fs.promises.rm(path.join(folder, '.wizard-source'), { force: true });
    expect((await target.lookup('photos', true))?.repo).toBe(OLD);
    await fs.promises.writeFile(path.join(folder, '.wizard-source'), JSON.stringify({ repo: 'humanitylabs-org/gone' }));
    expect((await target.lookup('photos', false))?.repo).toBe(OLD);
    expect(fetch).toHaveBeenCalledTimes(1); // the cached list is reused
  });
});
