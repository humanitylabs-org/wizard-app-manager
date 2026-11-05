import fsp from 'node:fs/promises';
import path from 'node:path';
import { APP_GENERATED_COMPOSE_FILENAME, APP_REL_COMPOSE_FILENAME } from '@/common/constants';
import { extractAppUrn } from '@/common/helpers/app-helpers';
import { execFileAsync } from '@/common/helpers/exec-helpers';
import { ConfigurationService } from '@/core/config/configuration.service';
import { FilesystemService } from '@/core/filesystem/filesystem.service';
import { LoggerService } from '@/core/logger/logger.service';
import { Injectable } from '@nestjs/common';
import { appInfoSchema, convertLegacyToYaml, dynamicComposeSchemaYaml } from '@runtipi/common/schemas';
import type { AppUrn } from '@runtipi/common/types';
import { type } from 'arktype';

/**
 * Folders inside an app's data folder that hold owner-only secrets, relative to it. setAppDataDirPermissions never
 * opens them to everyone. `data/wizard-os/grants`: the Agent Service gateway's grant keys
 * (`grants/agent-service.json`, wizard-os spec/PERMISSIONS.md layer 2), which the gateway ignores unless the file is
 * 0600; opening it on every start or update took every gateway-verified app's tools away from the agents.
 */
export const PRIVATE_APP_DATA_DIRS = ['data/wizard-os/grants'];

/** Normalize owner permissions and remove group/other access: directory 0700, regular files 0600; owners unchanged. */
async function tightenPrivateDir(appDataDir: string, relativeDir: string, warn: (message: string) => void) {
  const NOFOLLOW = fsp.constants.O_RDONLY | fsp.constants.O_NOFOLLOW | fsp.constants.O_NONBLOCK;
  const anchor = await fsp.open(appDataDir, NOFOLLOW | fsp.constants.O_DIRECTORY);
  const handles = [anchor];
  try {
    // Linux procfd anchoring is required. Fail closed if unavailable; never fall back to resolving app paths.
    const pinned = await anchor.stat();
    const proc = await fsp.stat(`/proc/self/fd/${anchor.fd}`);
    if (pinned.dev !== proc.dev || pinned.ino !== proc.ino) throw new Error('Private permissions require a verified procfd anchor');

    let directory = anchor;
    for (const name of relativeDir.split('/')) {
      if (!name || name === '.' || name === '..') throw new Error('Invalid private directory component');
      const next = await fsp.open(`/proc/self/fd/${directory.fd}/${name}`, NOFOLLOW | fsp.constants.O_DIRECTORY).catch((error: NodeJS.ErrnoException) => {
        // Missing directories and links (including the grants leaf) are safe no-ops.
        if (error.code === 'ENOENT' || error.code === 'ENOTDIR' || error.code === 'ELOOP') return null;
        throw error;
      });
      if (!next) return;
      handles.push(next);
      directory = next;
    }

    if (((await directory.stat()).mode & 0o7777) !== 0o700) await directory.chmod(0o700);
    let warnedHardlink = false;
    for (const entry of await fsp.readdir(`/proc/self/fd/${directory.fd}`, { withFileTypes: true })) {
      if (!entry.isFile()) continue;
      const file = await fsp.open(`/proc/self/fd/${directory.fd}/${entry.name}`, NOFOLLOW).catch(() => null);
      // O_RDONLY may fail for unreadable files; leave them unchanged rather than resolve an unpinned path.
      if (!file) continue;
      try {
        const st = await file.stat();
        if (!st.isFile()) continue;
        if (st.nlink !== 1) {
          if (!warnedHardlink) warn('Skipped hardlinked private grant files; permissions unchanged');
          warnedHardlink = true;
          continue;
        }
        if ((st.mode & 0o7777) !== 0o600) await file.chmod(0o600);
      } finally {
        await file.close();
      }
    }
  } finally {
    // Start every close even if another close fails, including partial walks and procfd verification errors.
    await Promise.all(handles.map((handle) => handle.close()));
  }
}

@Injectable()
export class AppFilesManager {
  constructor(
    private readonly configuration: ConfigurationService,
    private readonly filesystem: FilesystemService,
    private readonly logger: LoggerService,
  ) {}

  private getInstalledAppsFolder() {
    const { directories } = this.configuration.getConfig();

    return path.join(directories.dataDir, 'apps');
  }

  public getAppPaths(appUrn: AppUrn) {
    const { directories } = this.configuration.getConfig();

    const { appStoreId, appName } = extractAppUrn(appUrn);

    return {
      appDataDir: path.join(directories.appDataDir, appStoreId, appName),
      appInstalledDir: path.join(this.getInstalledAppsFolder(), appStoreId, appName),
    };
  }

  /**
   * Get the app info from the installed apps apps
   * @param id - The app id
   */
  public async getInstalledAppInfo(appUrn: AppUrn) {
    try {
      const { appInstalledDir } = this.getAppPaths(appUrn);
      const configPath = path.join(appInstalledDir, 'config.json');

      if (await this.filesystem.isFile(configPath)) {
        const configFile = await this.filesystem.readTextFile(configPath);

        const config = JSON.parse(configFile ?? '{}');
        const parsedConfig = appInfoSchema({ ...config, urn: appUrn });

        if (parsedConfig instanceof type.errors) {
          this.logger.error(`App ${appUrn} config error:`);
          this.logger.error(parsedConfig.summary);
          return null;
        }

        if (parsedConfig.available) {
          const descriptionPath = path.join(appInstalledDir, 'metadata', 'description.md');
          const description = (await this.filesystem.isFile(descriptionPath)) ? ((await this.filesystem.readTextFile(descriptionPath)) ?? '') : '';

          return { ...parsedConfig, description };
        }
      }
    } catch (_) {
      return null;
    }

    return null;
  }

  /**
   * Get the docker-compose.json file content from the installed app
   * @param appUrn - The app id
   * @returns The content of docker-compose.yml as a string, or null if not found
   */
  public async getGeneratedDockerComposeYaml(appUrn: AppUrn) {
    const { appInstalledDir } = this.getAppPaths(appUrn);
    const dockerComposePath = path.join(appInstalledDir, APP_GENERATED_COMPOSE_FILENAME);

    try {
      if (await this.filesystem.isFile(dockerComposePath)) {
        const content = await this.filesystem.readTextFile(dockerComposePath);
        return { path: dockerComposePath, content };
      }

      // Check for legacy generated file (docker-compose.yml without x-runtipi)
      const legacyPath = path.join(appInstalledDir, APP_REL_COMPOSE_FILENAME);
      if (await this.filesystem.isFile(legacyPath)) {
        const legacyContent = await this.filesystem.readYamlFile(legacyPath);
        // If it DOES NOT have x-runtipi, it is the old generated file
        if (legacyContent && typeof legacyContent === 'object' && !('x-runtipi' in legacyContent)) {
          const content = await this.filesystem.readTextFile(legacyPath);
          return { path: legacyPath, content };
        }
      }
    } catch (error) {
      this.logger.error(`Error getting ${APP_GENERATED_COMPOSE_FILENAME} for installed app ${appUrn}:`, error);
    }

    return { path: dockerComposePath, content: null };
  }

  /**
   * Get the source docker-compose.yml file content from the installed app
   * @param appUrn - The app id
   * @returns The content as a DynamicComposeSchemaYaml object and path
   */
  public async getSourceDockerComposeYaml(appUrn: AppUrn) {
    const { appInstalledDir } = this.getAppPaths(appUrn);

    // 1. Try new source name: docker-compose.yml
    const appYamlPath = path.join(appInstalledDir, APP_REL_COMPOSE_FILENAME);

    let content = null;

    try {
      if (await this.filesystem.isFile(appYamlPath)) {
        content = await this.filesystem.readYamlFile(appYamlPath);
      }
    } catch (error) {
      this.logger.error(`Error getting ${APP_REL_COMPOSE_FILENAME} for installed app ${appUrn}:`, error);
    }

    // Check if it has metadata (valid source file)
    if (content && typeof content === 'object' && 'x-runtipi' in content) {
      const compose = dynamicComposeSchemaYaml(content);

      if (compose instanceof type.errors) {
        throw new Error(`Invalid ${APP_REL_COMPOSE_FILENAME} format for app ${appUrn}.`);
      }

      return { path: appYamlPath, content: compose };
    }

    // 2. Fallback to legacy source: docker-compose.json
    const dockerComposeLegacyPath = path.join(appInstalledDir, 'docker-compose.json');

    try {
      if (await this.filesystem.isFile(dockerComposeLegacyPath)) {
        const jsonContent = await this.filesystem.readJsonFile(dockerComposeLegacyPath);

        const compose = dynamicComposeSchemaYaml(convertLegacyToYaml(jsonContent));

        if (compose instanceof type.errors) {
          this.logger.error(compose.summary);
          throw new Error(`Invalid docker-compose.json format for app ${appUrn}.`);
        }

        return { path: dockerComposeLegacyPath, content: compose };
      }
    } catch (error) {
      this.logger.error(`Error getting docker-compose.json for installed app ${appUrn}:`, error);
    }

    return { path: appYamlPath, content: null };
  }

  /**
   * Write the docker-compose.yml file to the installed app folder
   * @param appUrn - The app id
   * @param composeFile - The content of the docker-compose.yml file
   */
  public async writeDockerComposeYml(appUrn: AppUrn, composeFile: string) {
    const { appInstalledDir } = this.getAppPaths(appUrn);
    const dockerComposePath = path.join(appInstalledDir, APP_GENERATED_COMPOSE_FILENAME);

    if (!(await this.filesystem.writeTextFile(dockerComposePath, composeFile))) {
      throw new Error(`Failed to write generated docker compose file for app ${appUrn}`);
    }
  }

  public async deleteAppFolder(appUrn: AppUrn) {
    const { appInstalledDir } = this.getAppPaths(appUrn);
    return this.filesystem.removeDirectory(appInstalledDir);
  }

  public async deleteAppDataDir(appUrn: AppUrn) {
    const { appDataDir } = this.getAppPaths(appUrn);
    await this.filesystem.removeDirectory(appDataDir);
  }

  public async createAppDataDir(appUrn: AppUrn) {
    const { appDataDir } = this.getAppPaths(appUrn);
    await this.filesystem.createDirectory(appDataDir);
  }

  /**
   * Set the permissions for the app data directory: everything a+rwx (Runtipi's `chmod -Rf a+rwx`), except the
   * owner-only secret folders in PRIVATE_APP_DATA_DIRS, which are skipped and brought back to 0700 / 0600.
   * Private normalization uses pinned descriptors and does not follow links. The ordinary-data find/exec
   * stage below remains path-based and is not protected against concurrent directory replacement.
   * @param appUrn - The app id
   */
  public async setAppDataDirPermissions(appUrn: AppUrn) {
    const { appDataDir } = this.getAppPaths(appUrn);
    const glob = (p: string) => p.replace(/[[\]*?\\]/g, '\\$&');
    const skip = PRIVATE_APP_DATA_DIRS.flatMap((rel, i) => [...(i ? ['-o'] : []), '-path', glob(path.join(appDataDir, rel))]);
    const open = ['!', '-type', 'l', '-exec', 'chmod', '-f', 'a+rwx', '{}', '+'];

    await execFileAsync('find', ['-H', appDataDir, '(', ...skip, ')', '-prune', '-o', ...open]).catch(() => {
      this.logger.error(`Error setting permissions for app ${appUrn}`);
    });
    for (const rel of PRIVATE_APP_DATA_DIRS) {
      await tightenPrivateDir(appDataDir, rel, (message) => this.logger.warn(message)).catch(() => {
        this.logger.error(`Error restoring private permissions for app ${appUrn}`);
      });
    }
  }

  public async getAppEnv(appUrn: AppUrn) {
    const { appDataDir } = this.getAppPaths(appUrn);

    const envPath = path.join(appDataDir, 'app.env');

    let env: string | null = null;
    if (await this.filesystem.isFile(envPath)) {
      env = await this.filesystem.readTextFile(envPath);
      if (env === null) {
        throw new Error(`Failed to read app environment for ${appUrn}`);
      }
    }

    return { path: envPath, content: env };
  }

  public async writeAppEnv(appUrn: AppUrn, env: string) {
    const { appDataDir } = this.getAppPaths(appUrn);

    const envPath = path.join(appDataDir, 'app.env');

    return this.filesystem.writeTextFile(envPath, env);
  }

  /**
   * Get the user env file content
   * @param appUrn - The app id
   */
  public async getUserEnv(appUrn: AppUrn) {
    const { directories } = this.configuration.getConfig();

    const { appStoreId, appName } = extractAppUrn(appUrn);

    const userEnvFile = path.join(directories.dataDir, 'user-config', appStoreId, appName, 'app.env');
    let content = null;

    if (await this.filesystem.isFile(userEnvFile)) {
      content = await this.filesystem.readTextFile(userEnvFile);
    }

    return { path: userEnvFile, content };
  }

  /**
   * Get the user compose file content
   * @param appUrn - The app id
   */
  public async getUserComposeFile(appUrn: AppUrn) {
    const { directories } = this.configuration.getConfig();

    const { appStoreId, appName } = extractAppUrn(appUrn);

    const userComposeFile = path.join(directories.dataDir, 'user-config', appStoreId, appName, 'docker-compose.yml');
    let content = null;

    if (await this.filesystem.isFile(userComposeFile)) {
      content = await this.filesystem.readTextFile(userComposeFile);
    }

    return { path: userComposeFile, content };
  }

  /**
   * Get the config.json file content from the installed app
   * @param appUrn - The app id
   * @returns The content of config.json as a string, or null if not found
   */
  public async getConfigJson(appUrn: AppUrn) {
    const { appInstalledDir } = this.getAppPaths(appUrn);
    const configPath = path.join(appInstalledDir, 'config.json');

    let content = null;
    try {
      if (await this.filesystem.isFile(configPath)) {
        content = await this.filesystem.readJsonFile(configPath);
      }
    } catch (error) {
      this.logger.error(`Error getting config.json for installed app ${appUrn}:`, error);
    }

    return { path: configPath, content };
  }
}
