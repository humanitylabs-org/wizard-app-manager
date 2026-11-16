import type { AppStatus } from '@/core/database/drizzle/types';

/**
 * Big multi-container apps pull gigabytes and stop many containers for their backup, so an install, update,
 * backup or restore can run far past the 5-minute queue default (QUEUE_TIMEOUT_IN_MINUTES). If the caller gives
 * up early it records the app as failed (`stopped`, old version) while the command is still running (WAM#711).
 * These commands, and the statuses they hold, get at least LONG_COMMAND_MINUTES.
 */
export const LONG_COMMAND_MINUTES = 30;

export const LONG_APP_COMMANDS: readonly string[] = ['install', 'update', 'backup', 'restore'];

const LONG_STATUSES: readonly AppStatus[] = ['installing', 'updating', 'backing_up', 'restoring'];

export const longCommandMinutes = (eventsTimeout: number) => Math.max(eventsTimeout, LONG_COMMAND_MINUTES);

/** How long an app may sit in a transitional status before the periodic status sync overrides it from Docker. */
export const transitionalGraceSeconds = (status: AppStatus, eventsTimeout: number) =>
  (LONG_STATUSES.includes(status) ? longCommandMinutes(eventsTimeout) : eventsTimeout) * 60;
