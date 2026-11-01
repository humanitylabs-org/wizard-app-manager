import { LoggerService } from '@/core/logger/logger.service';
import { AppFilesManager } from '@/modules/apps/app-files-manager';
import { AppHelpers } from '@/modules/apps/app.helpers';
import { DockerService } from '@/modules/docker/docker.service';
import type { ModuleRef } from '@nestjs/core';
import type { AppUrn } from '@runtipi/common/types';
import type Dockerode from 'dockerode';
import { expect, test, vi } from 'vitest';
import { mock } from 'vitest-mock-extended';
import { StartAppCommand } from './start-app-command';

for (const skipEnv of [false, true]) {
  test(`start initializes before up and never resets live permissions (skipEnv=${skipEnv})`, async () => {
    const logger = mock<LoggerService>();
    const appFilesManager = mock<AppFilesManager>();
    const dockerService = mock<DockerService>();
    const appHelpers = mock<AppHelpers>();
    const docker = mock<Dockerode>();
    const urn = 'fixture:synthetic' as AppUrn;
    const order: string[] = [];
    appFilesManager.getInstalledAppInfo.mockResolvedValue({ id: 'fixture' } as never);
    appFilesManager.setAppDataDirPermissions.mockImplementation(async () => {
      order.push('initialize-only');
    });
    dockerService.composeApp.mockImplementation(async () => {
      order.push('up');
      return { success: true, stdout: '', stderr: '' };
    });
    appHelpers.generateEnvFile.mockImplementation(async () => {
      order.push('private-env');
    });
    const services = new Map<unknown, unknown>([
      [LoggerService, logger],
      [AppFilesManager, appFilesManager],
      [DockerService, dockerService],
      [AppHelpers, appHelpers],
    ]);
    const moduleRef = { get: (token: unknown) => services.get(token) } as ModuleRef;
    const command = new StartAppCommand(moduleRef, docker);
    vi.spyOn(command as StartAppCommand & { ensureAppDir: (...args: unknown[]) => Promise<void> }, 'ensureAppDir').mockImplementation(async () => {
      await appFilesManager.setAppDataDirPermissions(urn);
    });
    expect((await command.execute(urn, { skipEnv, skipPull: true })).success).toBe(true);
    expect(order).toEqual(skipEnv ? ['initialize-only', 'up'] : ['initialize-only', 'private-env', 'up']);
    expect(appFilesManager.setAppDataDirPermissions).toHaveBeenCalledTimes(1);
  });
}
