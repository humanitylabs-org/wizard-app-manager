import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { MandatorySeal, type SealRecord } from './mandatory-seal';
import { confined } from './wizard-storage';
import * as yaml from 'yaml';
const execFileStrict = promisify(execFile);
import { WizardStorage } from './wizard-storage';
import fs from 'node:fs/promises';
import path from 'node:path';
import { ConfigurationService } from '@/core/config/configuration.service';
import { FilesystemService } from '@/core/filesystem/filesystem.service';
import { LoggerService } from '@/core/logger/logger.service';
import { Injectable, InternalServerErrorException, type OnApplicationBootstrap, type OnModuleDestroy } from '@nestjs/common';
import type { AppUrn } from '@runtipi/common/types';
import * as Sentry from '@sentry/nestjs';
import { AppFilesManager } from '../apps/app-files-manager';
import { AppsService } from '../apps/apps.service';
import { AppsRepository } from '../apps/apps.repository';

@Injectable()
export class DockerService implements OnApplicationBootstrap, OnModuleDestroy {
  private grantsTimer?: NodeJS.Timeout;
  onApplicationBootstrap() {
    const run = () => this.storage.reconcile().catch((e) => this.logger.error('App grants reconcile failed', e));
    void run();
    this.grantsTimer = setInterval(() => void run(), 5 * 60 * 1000);
    this.grantsTimer.unref?.();
  }
  onModuleDestroy() {
    if (this.grantsTimer) clearInterval(this.grantsTimer);
  }
  constructor(
    private readonly logger: LoggerService,
    private readonly config: ConfigurationService,
    private readonly appFilesManager: AppFilesManager,
    private readonly filesystem: FilesystemService,
    private readonly appsService: AppsService,
    private readonly appsRepository: AppsRepository,
  ) {}

  /**
   * Get the base compose args for an app
   * @param {string} appUrn - App name
   */
  public getBaseComposeArgsApp = async (appUrn: AppUrn) => {
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
    const seal = new MandatorySeal(this.config.get('directories').dataDir);
    const enrollment = await seal.read(appUrn);
    const generated = await this.appFilesManager.getGeneratedDockerComposeYaml(appUrn);
    if (!generated.content) throw new Error(`Missing generated Compose for ${appUrn}`);
    await this.storage.prepare(appUrn, generated.content, async (prepared) => {
      if (prepared !== generated.content) {
        await fs.writeFile(`${generated.path}.tmp-storage`, prepared);
        await fs.rename(`${generated.path}.tmp-storage`, generated.path);
      }
      // Update's standalone preflight must validate the seal before backup/down.
      if (enrollment && !apply) await this.sealedLaunch(appUrn, seal, enrollment, async () => {});
      await apply?.();
    });
  }

  public async composeApp(appUrn: AppUrn, command: string, beforeStart?: () => Promise<void>) {
    const action = command.split(' ')[0];
    if (['start', 'restart', 'create', 'run', 'watch'].includes(action ?? '')) {
      // All supported lifecycle activation uses up. Never allow a future direct
      // Compose shortcut to skip the validated launch boundary for an enrollment.
      if (await new MandatorySeal(this.config.get('directories').dataDir).read(appUrn))
        throw new Error('Sealed activation requires the validated manager up lifecycle');
    }
    if (action === 'up') {
      // A trusted, sticky enrollment is independent of app/user-config state.
      // Check it before storage preparation or a restart's destructive callback.
      const seal = new MandatorySeal(this.config.get('directories').dataDir);
      const enrollment = await seal.read(appUrn);
      let result!: { success: boolean; stdout: string; stderr: string };
      await this.prepareApp(appUrn, async () => {
        const launch = async (args?: string[]) => {
          await beforeStart?.();
          result = await this.runComposeApp(appUrn, command, args);
        };
        if (enrollment) await this.sealedLaunch(appUrn, seal, enrollment, launch);
        else await launch();
      });
      return result;
    }
    return this.runComposeApp(appUrn, command);
  }

  private async sealedLaunch(appUrn: AppUrn, seal: MandatorySeal, enrollment: SealRecord, launch: (args: string[]) => Promise<void>) {
    const { args } = await this.getBaseComposeArgsApp(appUrn);
    const generated = await this.appFilesManager.getGeneratedDockerComposeYaml(appUrn);
    seal.validateCatalogue(enrollment, yaml.parse(generated.content ?? ''));
    const state = await confined(seal.dataDir, 'state/mandatory-seal-launches', true);
    const dir = await fs.mkdtemp(path.join(state, 'launch-'));
    const project = ['--project-name', appUrn.replace(':', '_'), '--project-directory', path.dirname(generated.path)];
    const required = path.join(dir, 'required.yml');
    const snapshot = path.join(dir, 'effective.json');
    try {
      await fs.writeFile(required, seal.overlay(enrollment), { mode: 0o600 });
      const resolve = async (input: string[]) => {
        try {
          // Dry config only: no container/network create, start, stop or pull.
          const { stdout } = await execFileStrict('docker-compose', [...input, 'config', '--format', 'json'], {
            encoding: 'utf8', timeout: 30_000, maxBuffer: 16 * 1024 * 1024,
          });
          return JSON.parse(stdout);
        } catch { throw new Error('Mandatory seal Compose validation failed; refusing lifecycle mutation'); }
      };
      const approved = await resolve([...project, '-f', required]);
      const effective = await resolve([...args, '--project-directory', path.dirname(generated.path), '-f', required]);
      seal.validateEffective(effective, approved);
      // Execute only these validated, fully resolved bytes. Optional files/env
      // cannot race a later re-read; escape literal dollars for Compose reinput.
      await fs.writeFile(snapshot, seal.snapshot(effective), { mode: 0o600 });
      await launch([...project, '-f', snapshot]);
    } finally {
      await fs.rm(dir, { recursive: true, force: true });
    }
  }

  private async runComposeApp(appUrn: AppUrn, command: string, sealedArgs?: string[]) {
    let { args, isCustomConfig } = sealedArgs
      ? { args: [...sealedArgs], isCustomConfig: false }
      : await this.getBaseComposeArgsApp(appUrn);
    args.push(...command.split(' '));
    args = args.filter(Boolean);

    this.logger.info(`Running docker compose with args ${args.join(' ')}`);

    const cmd = spawn('docker-compose', args);
    const stdout: string[] = [];
    const stderr: string[] = [];

    const exitCode = await new Promise((resolve, reject) => {
      cmd.once('error', reject);
      cmd.stdout.on('data', (data) => {
        this.logger.debug(`docker-compose: ${String(data).trim()}`);
        stdout.push(String(data).trim());
      });
      cmd.stderr.on('data', (data) => {
        this.logger.debug(`docker-compose: ${String(data).trim()}`);
        stderr.push(String(data).trim());
      });
      cmd.on('close', resolve);
    });

    if (exitCode !== 0) {
      this.logger.info(`Docker-compose exited with code ${exitCode}`);
      if (isCustomConfig) {
        this.logger.warn('User-config detected, please make sure your configuration is correct before opening an issue');
      }
      const error = stderr.pop();
      throw new Error(error);
    }

    return { success: true, stdout: stdout.join(''), stderr: stderr.join('') };
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
