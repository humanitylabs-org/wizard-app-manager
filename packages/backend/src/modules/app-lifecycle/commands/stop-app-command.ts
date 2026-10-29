import { LoggerService } from '@/core/logger/logger.service';
import { DockerService } from '@/modules/docker/docker.service';
import type { AppEventFormInput } from '@/modules/queue/entities/app-events';
import type { AppUrn } from '@runtipi/common/types';
import { AppLifecycleCommand } from './command';

export class StopAppCommand extends AppLifecycleCommand {
  public async execute(appUrn: AppUrn, _form: AppEventFormInput) {
    const logger = this.moduleRef.get(LoggerService, { strict: false });
    const dockerService = this.moduleRef.get(DockerService, { strict: false });

    try {
      // Teardown must remain available after revocation or recipe corruption.
      logger.info(`Stopping app ${appUrn}`);

      await dockerService.composeApp(appUrn, 'down --remove-orphans');

      logger.info(`App ${appUrn} stopped successfully`);

      return { success: true, message: `App ${appUrn} stopped successfully` };
    } catch (err) {
      return this.handleAppError(err, appUrn, 'stop');
    }
  }
}
