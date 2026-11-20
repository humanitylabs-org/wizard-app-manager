import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { WizardStorage } from './wizard-storage';
import { enforceAppNetworks } from './wizard-networks';
import { addStopInit } from './wizard-stop-init';
import { mergeArchitectureOverrides } from './builders/compose.builder';
import fs from 'node:fs/promises';
import path from 'node:path';
import { ConfigurationService } from '@/core/config/configuration.service';
import { FilesystemService } from '@/core/filesystem/filesystem.service';
import { LoggerService } from '@/core/logger/logger.service';
import { Inject, Injectable, InternalServerErrorException, type OnApplicationBootstrap, type OnModuleDestroy } from '@nestjs/common';
import { effectiveComposeSchema, type EffectiveAppServices } from './compose-config';
import type { AppUrn } from '@runtipi/common/types';
import * as Sentry from '@sentry/nestjs';
import { AppFilesManager, type AppMigration } from '../apps/app-files-manager';
import { AppsService } from '../apps/apps.service';
import { AppsRepository } from '../apps/apps.repository';
import type Dockerode from 'dockerode';
import { DOCKERODE } from './docker.constants';

@Injectable()
export class DockerService implements OnApplicationBootstrap, OnModuleDestroy {
  private grantsTimer?: NodeJS.Timeout;
  private requestsTimer?: NodeJS.Timeout;
  onApplicationBootstrap() {
    const run = () => this.storage.reconcile().catch((e) => this.logger.error('App grants reconcile failed', e));
    void run();
    this.grantsTimer = setInterval(() => void run(), 5 * 60 * 1000);
    this.grantsTimer.unref?.();
    // Connection requests apps leave at runtime (/wizard-requests/connections.json): picked up within seconds.
    let busy = false;
    this.requestsTimer = setInterval(() => {
      if (busy) return;
      busy = true;
      this.storage
        .collectRequests()
        .catch((e) => this.logger.error('Connection requests check failed', e))
        .finally(() => {
          busy = false;
        });
    }, 5000);
    this.requestsTimer.unref?.();
  }
  onModuleDestroy() {
    if (this.grantsTimer) clearInterval(this.grantsTimer);
    if (this.requestsTimer) clearInterval(this.requestsTimer);
  }
  constructor(
    private readonly logger: LoggerService,
    private readonly config: ConfigurationService,
    private readonly appFilesManager: AppFilesManager,
    private readonly filesystem: FilesystemService,
    private readonly appsService: AppsService,
    private readonly appsRepository: AppsRepository,
    @Inject(DOCKERODE) private readonly docker: Dockerode,
  ) {}

  private async getComposeEnvironment(appUrn: AppUrn) {
    let isCustomConfig = false;

    const appEnv = await this.appFilesManager.getAppEnv(appUrn);
    const args: string[] = ['--env-file', appEnv.path];

    const { app } = await this.appsService.getApp(appUrn);
    const userConfigEnabled = app?.userConfigEnabled ?? true;

    // User custom env file
    const userEnvFile = await this.appFilesManager.getUserEnv(appUrn);
    if (userEnvFile.content && userConfigEnabled) {
      isCustomConfig = true;
      args.push('--env-file', userEnvFile.path);
    }

    return { args, isCustomConfig, userConfigEnabled };
  }

  public getBaseComposeArgsApp = async (appUrn: AppUrn) => {
    const { args, isCustomConfig: customEnvironment, userConfigEnabled } = await this.getComposeEnvironment(appUrn);
    let isCustomConfig = customEnvironment;
    args.push('--project-name', appUrn.replace(':', '_'));

    const composeFile = await this.appFilesManager.getGeneratedDockerComposeYaml(appUrn);
    args.push('-f', composeFile.path);

    // User defined overrides
    const userComposeFile = await this.appFilesManager.getUserComposeFile(appUrn);
    if (userComposeFile.content && userConfigEnabled) {
      isCustomConfig = true;
      args.push('--file', userComposeFile.path);
    }

    return { args, isCustomConfig };
  };

  public getBaseComposeArgsRuntipi = async () => {
    const { dataDir } = this.config.get('directories');
    const args: string[] = ['--env-file', path.join(dataDir, '.env')];

    args.push('--project-name', 'runtipi');

    const composeFile = path.join(dataDir, 'docker-compose.yml');
    args.push('-f', composeFile);

    // User defined overrides
    const userComposeFile = path.join(dataDir, 'user-config', 'tipi-compose.yml');
    if (await this.filesystem.isFile(userComposeFile)) {
      args.push('--file', userComposeFile);
    }

    return { args };
  };

  /**
   * Helpers to execute docker compose commands
   * @param {string} appUrn - App name
   * @param {string} command - Command to execute
   */
  private storageInstance?: WizardStorage;
  get storage() {
    if (!this.storageInstance)
      this.storageInstance = new WizardStorage(
        this.config.get('directories'),
        () => this.appsRepository.getApps(),
        undefined,
        undefined,
        (message) => this.logger.warn(message),
      );
    return this.storageInstance;
  }

  public async prepareApp(appUrn: AppUrn, apply?: () => Promise<void>) {
    const generated = await this.appFilesManager.getGeneratedDockerComposeYaml(appUrn);
    if (!generated.content) throw new Error(`Missing generated Compose for ${appUrn}`);
    await this.storage.prepare(appUrn, generated.content, async (prepared) => {
      // App networks: every start of a Wizard app, from wam, the App Store views or Runtipi's UI (wizard-networks.ts).
      const app = await Promise.resolve()
        .then(() => this.appsRepository.getAppByUrn(appUrn))
        .catch(() => undefined);
      let publishedServices: string[] = [];
      if (appUrn.endsWith(':wizardapps') && (app?.exposed === true || app?.exposedLocal === true)) {
        const source = await this.appFilesManager.getSourceDockerComposeYaml(appUrn);
        if (!source.content) throw new Error(`Missing source Compose for published ${appUrn}`);
        const { services } = mergeArchitectureOverrides(source.content, this.config.get('architecture'));
        publishedServices = Object.entries(services)
          .filter(
            ([, service]) => service['x-runtipi']?.is_main === true && service['x-runtipi']?.internal_port && service.network_mode === undefined,
          )
          .map(([name]) => name);
      }
      // Python first processes get Docker's init so they stop on SIGTERM instead of after Docker's timeout (#738).
      const enforced = addStopInit(await enforceAppNetworks(appUrn, prepared, this.config.get('directories').dataDir, publishedServices));
      if (enforced !== generated.content) {
        await fs.writeFile(`${generated.path}.tmp-storage`, enforced);
        await fs.rename(`${generated.path}.tmp-storage`, generated.path);
      }
      await apply?.();
    });
  }

  public async composeApp(appUrn: AppUrn, command: string, beforeStart?: () => Promise<void>) {
    if (command.split(' ')[0] === 'up') {
      let result!: { success: boolean; stdout: string; stderr: string };
      await this.prepareApp(appUrn, async () => {
        // Validate storage/shares/bridge before a restart can stop anything,
        // holding the grant lock continuously through down and up.
        await beforeStart?.();
        result = await this.runComposeApp(appUrn, command);
      });
      return result;
    }
    return this.runComposeApp(appUrn, command);
  }

  private async runComposeApp(appUrn: AppUrn, command: string) {
    const { args, isCustomConfig } = await this.getBaseComposeArgsApp(appUrn);
    return this.runCompose([...args, ...command.split(' ').filter(Boolean)], isCustomConfig);
  }

  public async composeMigration(appUrn: AppUrn, migration: AppMigration, command: string) {
    const { args, isCustomConfig } = await this.getComposeEnvironment(appUrn);
    args.push('--project-name', this.getMigrationProjectName(appUrn), '--file', migration.path);

    return this.runCompose([...args, ...command.split(' ').filter(Boolean)], isCustomConfig, { ...process.env, COMPOSE_PROFILES: '' });
  }

  private getMigrationProjectName(appUrn: AppUrn) {
    return `runtipi-migration-${createHash('sha256').update(appUrn).digest('hex').slice(0, 16)}`;
  }

  public async stopAppMigrations(appUrn: AppUrn) {
    const filters = { label: [`com.docker.compose.project=${this.getMigrationProjectName(appUrn)}`] };
    const containers = await this.docker.listContainers({ all: true, filters });

    for (const container of containers) {
      await this.docker
        .getContainer(container.Id)
        .remove({ force: true })
        .catch((error: { statusCode?: number }) => {
          if (error.statusCode !== 404) throw error;
        });
    }

    try {
      const networks = await this.docker.listNetworks({ filters });
      for (const network of networks) {
        await this.docker
          .getNetwork(network.Id)
          .remove()
          .catch((error: { statusCode?: number }) => {
            if (error.statusCode !== 404) throw error;
          });
      }
    } catch (error) {
      this.logger.warn(`Failed to clean migration networks for ${appUrn}:`, error);
    }
  }

  private async runCompose(args: string[], isCustomConfig: boolean, environment = process.env) {
    this.logger.info(`Running docker compose with args ${args.join(' ')}`);

    const cmd = spawn('docker-compose', args, { env: environment });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];

    const exitCode = await new Promise((resolve, reject) => {
      cmd.stdout.on('data', (data) => {
        if (!args.includes('config')) this.logger.debug(`docker-compose: ${String(data).trim()}`);
        const chunk = Buffer.isBuffer(data) ? data : Buffer.from(String(data));
        stdout.push(chunk);
      });
      cmd.stderr.on('data', (data) => {
        this.logger.debug(`docker-compose: ${String(data).trim()}`);
        const chunk = Buffer.isBuffer(data) ? data : Buffer.from(String(data));
        stderr.push(chunk);
      });
      cmd.on('close', resolve);
      cmd.on('error', reject);
    });

    const stdoutText = Buffer.concat(stdout).toString();
    const stderrText = Buffer.concat(stderr).toString();

    if (exitCode !== 0) {
      this.logger.info(`Docker-compose exited with code ${exitCode}`);
      if (isCustomConfig) {
        this.logger.warn('User-config detected, please make sure your configuration is correct before opening an issue');
      }
      throw new Error(stderrText.trim());
    }

    return { success: true, stdout: stdoutText, stderr: stderrText };
  }

  public async getEffectiveAppServices(appUrn: AppUrn): Promise<EffectiveAppServices> {
    const result = await this.composeApp(appUrn, 'config --format json');

    return effectiveComposeSchema.assert(JSON.parse(result.stdout)).services;
  }

  public async getEffectiveMigrationServices(appUrn: AppUrn, migration: AppMigration): Promise<EffectiveAppServices> {
    const result = await this.composeMigration(appUrn, migration, 'config --format json');

    return effectiveComposeSchema.assert(JSON.parse(result.stdout)).services;
  }

  public getLogsStream = async (maxLines: number, appUrn?: AppUrn) => {
    try {
      const { args } = appUrn ? await this.getBaseComposeArgsApp(appUrn) : await this.getBaseComposeArgsRuntipi();

      args.push('logs', '--follow', '-n', maxLines.toString());

      const logs = spawn('docker-compose', args, { stdio: 'pipe' });

      logs.on('error', () => {
        logs.kill('SIGINT');
      });

      return {
        on: logs.stdout.on.bind(logs.stdout),
        kill: () => logs.kill('SIGINT'),
      };
    } catch (error) {
      this.logger.error('Error getting log stream', error);
      Sentry.captureException(error, { tags: { source: 'docker log stream', appUrn } });
      throw new InternalServerErrorException('Error getting log stream');
    }
  };
}
