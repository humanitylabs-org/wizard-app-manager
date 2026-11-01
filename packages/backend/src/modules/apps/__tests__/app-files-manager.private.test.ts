import type { ConfigurationService } from '@/core/config/configuration.service';
import { initializeAppDataDirectory, writeAppEnvironment } from '@/core/filesystem/app-data-permissions';
import type { FilesystemService } from '@/core/filesystem/filesystem.service';
import type { LoggerService } from '@/core/logger/logger.service';
import { beforeEach, expect, test, vi } from 'vitest';
import type { AppUrn } from '@runtipi/common/types';
import { AppFilesManager } from '../app-files-manager';

vi.mock('@/core/filesystem/app-data-permissions', () => ({
  initializeAppDataDirectory: vi.fn(),
  writeAppEnvironment: vi.fn(),
}));
let manager: AppFilesManager;
const urn = 'demo:store' as AppUrn;
beforeEach(() => {
  vi.mocked(initializeAppDataDirectory).mockReset().mockResolvedValue(undefined);
  vi.mocked(writeAppEnvironment).mockReset().mockResolvedValue(undefined);
  manager = new AppFilesManager(
    { getConfig: () => ({ directories: { appDataDir: '/synthetic/app-data' } }) } as unknown as ConfigurationService,
    {} as FilesystemService,
    { error: vi.fn() } as unknown as LoggerService,
  );
});
for (const phase of ['install', 'restart', 'update']) {
  test(`${phase} uses only the create-only initializer`, async () => {
    await manager.setAppDataDirPermissions(urn);
    expect(initializeAppDataDirectory).toHaveBeenCalledExactlyOnceWith('/synthetic/app-data', 'store', 'demo');
    expect(writeAppEnvironment).not.toHaveBeenCalled();
  });
}
test('directory symlink/permission failures propagate and abort lifecycle, not log-and-continue', async () => {
  vi.mocked(initializeAppDataDirectory).mockRejectedValue(new Error('Unsafe path'));
  await expect(manager.setAppDataDirPermissions(urn)).rejects.toThrow('Unsafe path');
});
test('app env uses private atomic writer; errors preserve the existing false-return contract', async () => {
  expect(await manager.writeAppEnv(urn, 'FAKE_ONLY=1')).toBe(true);
  expect(writeAppEnvironment).toHaveBeenCalledExactlyOnceWith('/synthetic/app-data', 'store', 'demo', 'FAKE_ONLY=1');
  vi.mocked(writeAppEnvironment).mockRejectedValue(new Error('Unsafe file'));
  expect(await manager.writeAppEnv(urn, 'FAKE_ONLY=2')).toBe(false);
});
test('URN traversal is rejected before invoking filesystem operations', async () => {
  await expect(manager.setAppDataDirPermissions('../escape:store' as never)).rejects.toThrow();
  expect(initializeAppDataDirectory).not.toHaveBeenCalled();
});
