import type { AppUrn } from '@runtipi/common/types';
import { expect, it } from 'vitest';
import { mock } from 'vitest-mock-extended';
import { AppsRepository } from '../../apps/apps.repository';
import { AppEventsQueue, appEventResultSchema } from '../../queue/entities/app-events';
import { StatusManagerService } from '../services/status-manager.service';
import { RestartAppHandler } from './restart-app.handler';
import { StartAppHandler } from './start-app.handler';

it.each([RestartAppHandler, StartAppHandler])('%s preserves running status after a pre-stop refusal', async (Handler) => {
  const repository = mock<AppsRepository>();
  const queue = mock<AppEventsQueue>();
  const statuses = mock<StatusManagerService>();
  const urn = 'wizard-claude:core' as AppUrn;
  repository.getAppByUrn.mockResolvedValue({ id: 1, status: 'running', config: {} } as never);
  // Verify the flag survives the real queue schema, not just a hand-built mock.
  const result = appEventResultSchema.assert({ success: false, message: 'storage refused', deploymentUnchanged: true });
  queue.publish.mockResolvedValue(result);
  await new Handler(repository, queue, statuses).execute(urn);
  await Promise.resolve();
  expect(statuses.emitError).toHaveBeenCalledWith(expect.objectContaining({ status: 'running', error: 'storage refused' }));
});

it.each([RestartAppHandler, StartAppHandler])('%s does not claim running after deployment was changed', async (Handler) => {
  const repository = mock<AppsRepository>();
  const queue = mock<AppEventsQueue>();
  const statuses = mock<StatusManagerService>();
  repository.getAppByUrn.mockResolvedValue({ id: 1, status: 'running', config: {} } as never);
  queue.publish.mockResolvedValue({ success: false, message: 'Docker failed', deploymentUnchanged: false });
  await new Handler(repository, queue, statuses).execute('wizard-claude:core' as AppUrn);
  await Promise.resolve();
  expect(statuses.emitError).toHaveBeenCalledWith(expect.objectContaining({ status: 'stopped' }));
});
