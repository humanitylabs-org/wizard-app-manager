import { LoggerService } from '@/core/logger/logger.service';
import { AppFilesManager } from '@/modules/apps/app-files-manager';
import { DockerService } from '@/modules/docker/docker.service';
import type { AppUrn } from '@runtipi/common/types';
import { AppLifecycleCommand } from './command';

export class UninstallAppCommand extends AppLifecycleCommand {
  public async execute(appUrn: AppUrn): Promise<{ success: boolean; message: string }> {
    const logger = this.moduleRef.get(LoggerService, { strict: false });
    const appFilesManager = this.moduleRef.get(AppFilesManager, { strict: false });
    const dockerService = this.moduleRef.get(DockerService, { strict: false });

    try {
      logger.info(`Uninstalling app ${appUrn}`);

      const removedId = appUrn.split(':')[0]!;
      const installed = await dockerService.storage.apps();
      const affected = await dockerService.storage.remove(removedId);
      // Provider deletion must detach consumers before deleting provider-owned files.
      for (const app of installed.filter((a) => affected.includes(a.manifest.id) && a.urn !== appUrn)) {
        await dockerService.composeApp(app.urn as AppUrn, 'down --remove-orphans');
        await dockerService.composeApp(app.urn as AppUrn, 'up --detach --force-recreate --remove-orphans');
      }
      try {
        // Never delete the legacy Docker volume: it is the owner's rollback copy.
        await dockerService.composeApp(appUrn, 'down --remove-orphans --rmi all');
        logger.info(`Successfully cleaned up all Docker resources for ${appUrn}`);
      } catch (err) {
        logger.warn('Error taking down app', appUrn, err);
      }

      await appFilesManager.deleteAppFolder(appUrn);
      await appFilesManager.deleteAppDataDir(appUrn);

      return { success: true, message: `App ${appUrn} uninstalled successfully` };
    } catch (err) {
      return this.handleAppError(err, appUrn, 'uninstall');
    }
  }
}
