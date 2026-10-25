import { expect, it, vi } from 'vitest';
import { mock } from 'vitest-mock-extended';
import type { AppUrn } from '@runtipi/common/types';
import { AppsRepository } from '../../apps/apps.repository';
import { MarketplaceService } from '../../marketplace/marketplace.service';
import { AppEventsQueue } from '../../queue/entities/app-events';
import { StatusManagerService } from '../services/status-manager.service';
import { AppValidationService } from '../services/app-validation.service';
import { StartAppHandler } from './start-app.handler';
import { InstallAppHandler } from './install-app.handler';
import { AppLifecycleService } from '../app-lifecycle.service';
import { AsyncMutex } from '@/utils/mutex/async-mutex';
import { AppStatusSyncService } from '../app-status-sync.service';
import { ConfigurationService } from '@/core/config/configuration.service';
import { LoggerService } from '@/core/logger/logger.service';
import { SSEService } from '@/core/sse/sse.service';
import { SystemEventsQueue } from '@/modules/queue/entities/system-events';
import type Dockerode from 'dockerode';

const urn = 'wizard-claude:wizardapps' as AppUrn;
function fixture() {
  const repository = mock<AppsRepository>();
  const queue = mock<AppEventsQueue>();
  const status = mock<StatusManagerService>();
  const validation = mock<AppValidationService>();
  const marketplace = mock<MarketplaceService>();
  const start = mock<StartAppHandler>();
  marketplace.getAppInfoFromAppStoreOrInstalled.mockResolvedValue({ port: 8799, tipi_version: 1 } as never);
  repository.createApp.mockResolvedValue({ id: 1 } as never);
  const handler = new InstallAppHandler(repository, queue, status, validation, marketplace, start);
  return { repository, queue, status, start, handler };
}

it('does not delete a slow install when its RPC expires, then accepts the real completion', async () => {
  const { repository, queue, status, handler } = fixture();
  queue.publish.mockResolvedValue({ success: false, message: 'RPC_TIMEOUT: down is still running' });
  await handler.execute(urn, { form: {} });
  await Promise.resolve();
  expect(status.deleteApp).not.toHaveBeenCalled();
  expect(status.emitSuccess).not.toHaveBeenCalled();
  repository.getAppByUrn.mockResolvedValue({ id: 1, status: 'installing' } as never);
  await handler.complete(urn, { success: true, message: 'compose up finished' });
  expect(status.emitSuccess).toHaveBeenCalledWith(expect.objectContaining({ appId: 1, status: 'running' }));
  expect(status.deleteApp).not.toHaveBeenCalled();
});

it('logs the actual install failure and removes the row only after worker completion', async () => {
  const { repository, status, handler } = fixture();
  repository.getAppByUrn.mockResolvedValue({ id: 1, status: 'installing' } as never);
  await handler.complete(urn, { success: false, message: 'actual storage failure' });
  expect(status.emitError).toHaveBeenCalledWith(expect.objectContaining({ error: 'actual storage failure', event: 'install_error' }));
  expect(status.deleteApp).toHaveBeenCalledWith(1);
  expect(status.emitError.mock.invocationCallOrder[0]).toBeLessThan(status.deleteApp.mock.invocationCallOrder[0]!);
});

it('does not start or enqueue a second operation for an install still in progress', async () => {
  const { repository, queue, start, handler } = fixture();
  repository.getAppByUrn.mockResolvedValue({ id: 1, status: 'installing' } as never);
  await expect(handler.execute(urn, { form: {} })).rejects.toThrow('Install still in progress');
  expect(queue.publish).not.toHaveBeenCalled();
  expect(start.execute).not.toHaveBeenCalled();
});

it('does not let status sync invent completion for an install older than the RPC timeout', async () => {
  const repository = mock<AppsRepository>();
  const config = mock<ConfigurationService>();
  const docker = mock<Dockerode>();
  config.get.calledWith('userSettings').mockReturnValue({ eventsTimeout: 5 } as never);
  repository.getApps.mockResolvedValue([{ id: 1, appName: 'wizard-claude', appStoreSlug: 'wizardapps', status: 'installing', updatedAt: 0 }] as never);
  docker.listContainers.mockResolvedValue([]);
  const sync = new AppStatusSyncService(mock<LoggerService>(), repository, mock<SSEService>(), mock<SystemEventsQueue>(), config, docker);
  await sync.syncAllAppStatuses();
  expect(repository.updateAppById).not.toHaveBeenCalled();
});

it('serializes different apps through slow down/up and commits status before replying', async () => {
  let finish!: () => void;
  const slow = new Promise<void>((resolve) => { finish = resolve; });
  const seen: string[] = [];
  const service = Object.assign(Object.create(AppLifecycleService.prototype), {
    mutex: new AsyncMutex(),
    logger: { error: vi.fn() },
    commandFactory: { createCommand: () => ({ execute: async (app: string) => {
      seen.push(`start:${app}`);
      if (app === urn) await slow;
      seen.push(`end:${app}`);
      return { success: true, message: 'finished' };
    } }) },
    installAppHandler: { complete: async (app: string) => { seen.push(`commit:${app}`); } },
  }) as AppLifecycleService;
  const run = (appUrn: AppUrn) => service.invokeCommand({ appUrn, command: 'install', form: {}, requestId: crypto.randomUUID() }, async () => { seen.push(`reply:${appUrn}`); });
  const a = run(urn);
  const b = run('browser:wizardapps' as AppUrn);
  await Promise.resolve(); await Promise.resolve();
  expect(seen).toEqual([`start:${urn}`]);
  finish();
  await Promise.all([a, b]);
  expect(seen).toEqual([`start:${urn}`, `end:${urn}`, `commit:${urn}`, `reply:${urn}`, 'start:browser:wizardapps', 'end:browser:wizardapps', 'commit:browser:wizardapps', 'reply:browser:wizardapps']);
});
