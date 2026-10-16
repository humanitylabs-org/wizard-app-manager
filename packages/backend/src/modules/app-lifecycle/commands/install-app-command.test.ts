import { LoggerService } from '@/core/logger/logger.service';
import { AppFilesManager } from '@/modules/apps/app-files-manager';
import { AppHelpers } from '@/modules/apps/app.helpers';
import { DockerService } from '@/modules/docker/docker.service';
import { EnvUtils } from '@/modules/env/env.utils';
import { MarketplaceService } from '@/modules/marketplace/marketplace.service';
import { convertLegacyToYaml } from '@runtipi/common/schemas';
import type { AppUrn } from '@runtipi/common/types';
import type { ModuleRef } from '@nestjs/core';
import type Dockerode from 'dockerode';
import { expect, test, vi } from 'vitest';
import { mock } from 'vitest-mock-extended';
import { InstallAppCommand } from './install-app-command';

const appUrn = 'app:test' as AppUrn;

const createCommand = () => {
  const logger = mock<LoggerService>();
  const appFilesManager = mock<AppFilesManager>();
  const dockerService = mock<DockerService>();
  const marketplaceService = mock<MarketplaceService>();
  const appHelpers = mock<AppHelpers>();
  const envUtils = mock<EnvUtils>();
  const docker = mock<Dockerode>();

  const compose = convertLegacyToYaml({
    schemaVersion: 2,
    services: [{ name: 'app', image: 'ghcr.io/example/local-only:1', isMain: true, internalPort: 80 }],
  });
  marketplaceService.getSourceDockerComposeYaml.mockResolvedValue({ path: '/app/apps/test/docker-compose.yml', content: compose });
  marketplaceService.copyAppFromRepoToInstalled.mockResolvedValue(undefined);
  marketplaceService.copyDataDir.mockResolvedValue(undefined);
  appHelpers.generateEnvFile.mockResolvedValue(undefined);
  appFilesManager.getAppEnv.mockResolvedValue({ path: '/data/app-data/test/app.env', content: 'A=1' });
  appFilesManager.getInstalledAppInfo.mockResolvedValue({ id: 'test' } as never);
  appFilesManager.setAppDataDirPermissions.mockResolvedValue(undefined);
  envUtils.envStringToMap.mockReturnValue(new Map());
  dockerService.composeApp.mockResolvedValue({ success: true, stdout: '', stderr: '' });

  const services = new Map<unknown, unknown>([
    [LoggerService, logger],
    [AppFilesManager, appFilesManager],
    [DockerService, dockerService],
    [MarketplaceService, marketplaceService],
    [AppHelpers, appHelpers],
    [EnvUtils, envUtils],
  ]);
  const moduleRef = { get: (token: unknown) => services.get(token) } as ModuleRef;
  const command = new InstallAppCommand(moduleRef, docker);
  vi.spyOn(command as InstallAppCommand & { ensureAppDir: (...args: unknown[]) => Promise<void> }, 'ensureAppDir').mockResolvedValue(undefined);
  return { command, dockerService };
};

test('install clears old containers but never deletes the app images (locally loaded images have no registry)', async () => {
  const { command, dockerService } = createCommand();

  const result = await command.execute(appUrn, {});

  expect(result.success).toBe(true);
  const calls = dockerService.composeApp.mock.calls.map(([, args]) => args);
  expect(calls[0]).toBe('down --remove-orphans');
  expect(calls.some((args) => args.includes('--rmi'))).toBe(false);
  expect(calls.at(-1)).toMatch(/^up --detach --force-recreate --remove-orphans/);
});
