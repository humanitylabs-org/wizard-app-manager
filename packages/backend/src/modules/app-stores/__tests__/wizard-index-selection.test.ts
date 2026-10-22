import fs from 'node:fs';
import path from 'node:path';
import { DATA_DIR } from '@/common/constants';
import type { ConfigurationService } from '@/core/config/configuration.service';
import type { AppStore } from '@/core/database/drizzle/types';
import { FilesystemService } from '@/core/filesystem/filesystem.service';
import type { LoggerService } from '@/core/logger/logger.service';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { WizardIndexStore } from '../wizard-index-store';

describe('owner-local exact Store selections', () => {
  let manager: WizardIndexStore;
  const folder = path.join(DATA_DIR, 'repos', 'wizardapps', 'apps', 'demo');
  const sha = 'a'.repeat(40);
  beforeEach(async () => {
    const logger = {debug:vi.fn(),error:vi.fn(),info:vi.fn(),warn:vi.fn()} as unknown as LoggerService;
    const configuration = {get:vi.fn().mockReturnValue({dataDir:DATA_DIR}),getConfig:vi.fn().mockReturnValue({directories:{dataDir:DATA_DIR}})} as unknown as ConfigurationService;
    manager = new WizardIndexStore(configuration, new FilesystemService(logger), logger, {slug:'wizardapps',url:'https://index.test'} as AppStore);
    await fs.promises.mkdir(folder,{recursive:true});
    await fs.promises.writeFile(path.join(folder,'.wizard-commit'),sha+'\n');
    await fs.promises.writeFile(path.join(folder,'.wizard-source'),JSON.stringify({repo:'org/demo'}));
    await fs.promises.writeFile(path.join(folder,'.wizard-selection'),JSON.stringify({repo:'org/demo',commit:sha}));
  });
  it('never consults or overwrites with the index even on a fresh copy lookup', async () => {
    const lookup=vi.spyOn(globalThis,'fetch').mockRejectedValue(Error('must not contact index'));
    await manager.ensureApp('demo',true);
    expect(lookup).not.toHaveBeenCalled();
    expect(await fs.promises.readFile(path.join(folder,'.wizard-commit'),'utf8')).toBe(sha+'\n');
    lookup.mockRestore();
  });
  it('rejects a corrupted selected pin rather than falling back', async () => {
    await fs.promises.writeFile(path.join(folder,'.wizard-selection'),JSON.stringify({repo:'foreign/demo',commit:sha}));
    await expect(manager.ensureApp('demo',true)).rejects.toThrow('invalid owner-local selection pin');
  });
  it('rejects a stale selected recipe commit', async () => {
    await fs.promises.writeFile(path.join(folder,'.wizard-commit'),'b'.repeat(40));
    await expect(manager.ensureApp('demo',true)).rejects.toThrow('invalid owner-local selection pin');
  });
});
