import fs from 'node:fs';
import path from 'node:path';
import type { MigrationLedgerState } from '@/core/database/drizzle/schema';
import { FilesystemService } from '@/core/filesystem/filesystem.service';
import type { LoggerService } from '@/core/logger/logger.service';
import type { AppsRepository } from '@/modules/apps/apps.repository';
import type { AppUrn } from '@runtipi/common/types';
import { fromPartial } from '@total-typescript/shoehorn';
import { describe, expect, it, vi } from 'vitest';
import { mock } from 'vitest-mock-extended';
import { RestoreTransaction } from '../restore-transaction';

vi.unmock('fs');
vi.unmock('node:fs');

const state: MigrationLedgerState = { initialized: true, migrationIds: [] };

const transactionFor = (destinations: string[]) => {
  const logger = mock<LoggerService>();
  const repository = mock<AppsRepository>();
  let journal: unknown;
  repository.getMigrationState.mockResolvedValue(state);
  repository.createRestoreJournal.mockImplementation(async (_urn, previousState, targetState, directories) => {
    journal = fromPartial({ phase: 'prepared', previousState, targetState, directories });
  });
  repository.getRestoreJournal.mockImplementation(async () => journal as never);
  repository.finishRestore.mockResolvedValue(undefined as never);
  repository.clearRestoreJournal.mockImplementation(async () => {
    journal = undefined;
  });
  return new RestoreTransaction('wizard-calendar:wizardapps' as AppUrn, destinations, new FilesystemService(logger), repository, logger);
};

const mode = async (p: string) => ((await fs.promises.stat(p)).mode & 0o777).toString(8);

// #1053: Runtipi's restore staged every folder with mkdtemp (0700) and copied the backup into it, so a rolled-back
// app's recipe folder was left root-only and the box user could no longer read that app's wizard.md.
describe('RestoreTransaction on a real filesystem', () => {
  it.runIf(process.platform === 'linux')('gives the restored folders their archived mode, at least 0755', async () => {
    const root = await fs.promises.mkdtemp('/tmp/runtipi-restore-mode-');
    try {
      const extracted = path.join(root, 'extracted');
      const names = ['app-data', 'app', 'user-config'];
      for (const name of names) {
        await fs.promises.mkdir(path.join(extracted, name, 'sub'), { recursive: true });
        await fs.promises.writeFile(path.join(extracted, name, 'wizard.md'), '---\nid: wizard-calendar\n---\n');
        await fs.promises.chmod(path.join(extracted, name, 'sub'), 0o700); // a private subfolder stays private
      }
      await fs.promises.chmod(path.join(extracted, 'app'), 0o755);
      await fs.promises.chmod(path.join(extracted, 'app-data'), 0o750);
      await fs.promises.chmod(path.join(extracted, 'user-config'), 0o700); // a backup taken of an already broken box
      const destinations = names.map((name) => path.join(root, 'live', name));
      for (const destination of destinations) {
        await fs.promises.mkdir(destination, { recursive: true });
      }

      await transactionFor([...destinations, path.join(root, 'live', 'none')]).restore(
        [
          ...names.map((name, i) => ({ source: path.join(extracted, name), destination: destinations[i] as string, error: 'copy failed' })),
          { source: null, destination: path.join(root, 'live', 'none'), error: 'copy failed' },
        ],
        state,
      );

      expect(await mode(path.join(root, 'live', 'app'))).toBe('755');
      expect(await mode(path.join(root, 'live', 'user-config'))).toBe('755');
      expect(await mode(path.join(root, 'live', 'app-data'))).toBe('755');
      expect(await mode(path.join(root, 'live', 'none'))).toBe('755');
      expect(await mode(path.join(root, 'live', 'app', 'sub'))).toBe('700');
      await expect(fs.promises.readFile(path.join(root, 'live', 'app', 'wizard.md'), 'utf8')).resolves.toContain('wizard-calendar');
    } finally {
      await fs.promises.rm(root, { recursive: true, force: true });
    }
  });
});
