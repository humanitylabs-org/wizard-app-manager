import fs from 'node:fs';
import path from 'node:path';
import { DATA_DIR } from '@/common/constants';
import { beforeEach, vi } from 'vitest';
import type { FsMock } from './__mocks__/fs';

vi.mock('fs', async () => {
  const { fsMock } = await import('./__mocks__/fs');
  return {
    ...fsMock,
  };
});

vi.mock('node:sqlite', () => {
  const sqlite = require('node:sqlite');
  return sqlite;
});

vi.mock('@/utils/cooldown/cooldown', () => ({
  Cooldown: () => vi.fn().mockImplementation((fn) => fn),
}));

beforeEach(async () => {
  // Security suites explicitly opt into real fs for checked-in fixtures and
  // inherited flock descriptors. Never seed DATA_DIR on the real filesystem.
  if (typeof (fs as unknown as FsMock).__resetAllMocks !== 'function') return;
  (fs as unknown as FsMock).__resetAllMocks();

  const directories = [DATA_DIR, path.join(DATA_DIR, 'state'), path.join(DATA_DIR, 'backups')];

  try {
    await Promise.all(
      directories.map(async (dir) => {
        await fs.promises.mkdir(dir, { recursive: true });
      }),
    );

    await fs.promises.writeFile(path.join(DATA_DIR, 'state', 'seed'), 'seed');
    await fs.promises.writeFile(path.join(DATA_DIR, '.env'), 'ROOT_FOLDER_HOST=/opt/runtipi');
  } catch (err) {
    console.error('Failed to setup test directories', err);
  }
});
