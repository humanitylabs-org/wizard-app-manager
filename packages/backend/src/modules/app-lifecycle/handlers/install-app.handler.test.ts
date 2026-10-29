import { fromPartial } from '@total-typescript/shoehorn';
import { describe, expect, it } from 'vitest';
import { mock } from 'vitest-mock-extended';
import type { AppUrn } from '@runtipi/common/types';
import { AppsRepository } from '../../apps/apps.repository';
import { AppEventsQueue } from '../../queue/entities/app-events';
import { MarketplaceService } from '../../marketplace/marketplace.service';
import { AppValidationService } from '../services/app-validation.service';
import { StatusManagerService } from '../services/status-manager.service';
import { StartAppHandler } from './start-app.handler';
import { InstallAppHandler } from './install-app.handler';

const urn = 'fixture:synthetic' as AppUrn;

// Real handler and real ArkType form schema; isolated dependencies. Not an
// HTTP/queue/DB acceptance test. Queue resolution is explicitly awaited below.
function fixture() {
  const repository = mock<AppsRepository>();
  repository.getAppByUrn.mockResolvedValue(undefined);
  repository.createApp.mockResolvedValue(fromPartial({ id: 7 }));
  const queue = mock<AppEventsQueue>();
  const status = mock<StatusManagerService>();
  const marketplace = mock<MarketplaceService>();
  marketplace.getAppInfoFromAppStoreOrInstalled.mockResolvedValue(fromPartial({ port: 10240, tipi_version: 1 }));
  const validation = mock<AppValidationService>();
  const start = mock<StartAppHandler>();
  let resolveQueue!: (result: { success: boolean; message: string }) => void;
  queue.publish.mockReturnValue(new Promise(resolve => { resolveQueue = resolve; }));
  let complete!: () => void;
  const completed = new Promise<void>(resolve => { complete = resolve; });
  status.emitSuccess.mockImplementation(async () => { complete(); });
  return { repository, queue, status, start, validation, resolveQueue, completed,
    handler: new InstallAppHandler(repository, queue, status, validation, marketplace, start) };
}

describe('install skipRun admission', () => {
  it.each([
    ['HTTP install-only', { skipRun: true }, undefined, true],
    ['explicit HTTP launch', { skipRun: false }, undefined, false],
    ['ordinary default', {}, undefined, false],
    ['trusted launch override', { skipRun: true }, false, false],
    ['trusted install-only override', { skipRun: false }, true, true],
    ['trusted install-only without form flag', {}, true, true],
  ] as const)('%s keeps queue and terminal status consistent', async (_name, form, override, expected) => {
    const f = fixture();
    await f.handler.execute(urn, { form: { ...form, openPort: false, skipPull: true }, skipRun: override });
    expect(f.queue.publish).toHaveBeenCalledWith(expect.objectContaining({
      appUrn: urn, command: 'install',
      form: expect.objectContaining({ skipRun: expected, skipPull: true, openPort: false }),
    }));
    expect(f.start.execute).not.toHaveBeenCalled();
    f.resolveQueue({ success: true, message: 'synthetic' });
    await f.completed;
    expect(f.status.emitSuccess).toHaveBeenCalledWith(expect.objectContaining({ status: expected ? 'stopped' : 'running' }));
  });

  it('invalid form never creates a record or publishes a launch even with trusted override', async () => {
    const f = fixture();
    await expect(f.handler.execute(urn, { form: { skipRun: 'true' }, skipRun: true })).rejects.toThrow();
    expect(f.repository.createApp).not.toHaveBeenCalled();
    expect(f.queue.publish).not.toHaveBeenCalled();
    expect(f.start.execute).not.toHaveBeenCalled();
  });
});
