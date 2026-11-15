import { type } from 'arktype';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mock } from 'vitest-mock-extended';
import type { ConfigurationService } from '@/core/config/configuration.service';
import type { LoggerService } from '@/core/logger/logger.service';
import { QueueFactory } from './queue.factory';

const rabbit = vi.hoisted(() => ({
  createConsumer: vi.fn(),
  createPublisher: vi.fn(() => ({ close: vi.fn() })),
  createRPCClient: vi.fn((props: { timeout?: number }) => ({ props, send: vi.fn(async () => ({ body: { success: true, message: 'ok' } })) })),
  on: vi.fn((event: string, callback: () => void) => {
    if (event === 'connection') {
      callback();
    }
  }),
  ready: true,
}));

vi.mock('rabbitmq-client', () => ({
  Connection: vi.fn(function Connection() {
    return rabbit;
  }),
}));

describe('QueueFactory', () => {
  const configurationService = mock<ConfigurationService>();
  const loggerService = mock<LoggerService>();

  beforeEach(() => {
    vi.clearAllMocks();

    configurationService.get.calledWith('queue').mockReturnValue({
      host: 'localhost',
      password: 'guest',
      username: 'guest',
    });
  });

  it('declares durable RPC and consumer queues', async () => {
    const queueFactory = new QueueFactory(loggerService, configurationService);

    const queue = await queueFactory.createQueue({
      queueName: 'app-events-queue',
      eventSchema: type({ event: 'string' }),
    });

    expect(rabbit.createRPCClient).toHaveBeenCalledWith(
      expect.objectContaining({
        queues: [{ autoDelete: false, durable: true, queue: 'app-events-queue' }],
      }),
    );

    queue.onEvent(vi.fn());

    expect(rabbit.createConsumer).toHaveBeenCalledWith(
      expect.objectContaining({
        queue: 'app-events-queue',
        queueOptions: { autoDelete: false, durable: true },
      }),
      expect.any(Function),
    );
  });

  it('never retries a command: a timed-out update must not run again (WAM#711)', async () => {
    const queueFactory = new QueueFactory(loggerService, configurationService);

    await queueFactory.createQueue({ queueName: 'q', eventSchema: type({ command: 'string' }), timeout: 1000 });
    await queueFactory.createQueue({
      queueName: 'q2',
      eventSchema: type({ command: 'string' }),
      timeout: 1000,
      longCommands: ['update'],
      longTimeout: 5000,
    });

    expect(rabbit.createRPCClient).toHaveBeenCalledTimes(3);
    for (const [props] of rabbit.createRPCClient.mock.calls as unknown as [{ maxAttempts: number }][]) {
      expect(props.maxAttempts).toBe(1);
    }
  });

  it('sends long commands (update, install) with the long timeout and the rest with the normal one', async () => {
    const queueFactory = new QueueFactory(loggerService, configurationService);

    const queue = await queueFactory.createQueue({
      queueName: 'app-events-queue',
      eventSchema: type({ command: 'string' }),
      timeout: 5 * 60_000,
      longCommands: ['install', 'update'],
      longTimeout: 30 * 60_000,
    });
    type Client = { props: { timeout: number }; send: ReturnType<typeof vi.fn> };
    const normal = rabbit.createRPCClient.mock.results[0]?.value as Client;
    const long = rabbit.createRPCClient.mock.results[1]?.value as Client;
    expect(normal.props.timeout).toBe(5 * 60_000);
    expect(long.props.timeout).toBe(30 * 60_000);

    await queue.publish({ command: 'update' });
    await queue.publish({ command: 'install' });
    await queue.publish({ command: 'start' });

    expect(long.send).toHaveBeenCalledTimes(2);
    expect(normal.send).toHaveBeenCalledTimes(1);
    expect(normal.send).toHaveBeenCalledWith('app-events-queue', { command: 'start' });
  });

  it('uses one client when no long commands are given', async () => {
    const queueFactory = new QueueFactory(loggerService, configurationService);

    const queue = await queueFactory.createQueue({ queueName: 'repo-queue', eventSchema: type({ command: 'string' }), timeout: 1000 });

    expect(rabbit.createRPCClient).toHaveBeenCalledTimes(1);
    await queue.publish({ command: 'update' });
    expect((rabbit.createRPCClient.mock.results[0]?.value as { send: ReturnType<typeof vi.fn> }).send).toHaveBeenCalledTimes(1);
  });
});
