import { type } from 'arktype';
import { AMQPError } from 'rabbitmq-client';
import { describe, expect, it, vi } from 'vitest';
import { Queue } from './queue.entity';

// WAM#711: an RPC timeout does not stop the command. The publisher must report the command's real result once it
// finishes, not "failed" while it still runs (which left a big update recorded `stopped` on its old version).
const timeoutError = () =>
  new (AMQPError as unknown as new (code: string, message: string) => AMQPError)('RPC_TIMEOUT', 'RPC response timed out');

const make = (send: (...a: unknown[]) => Promise<{ body: unknown }>) => {
  let handler: ((req: { body: unknown }, reply: (r: unknown) => Promise<void>) => Promise<void>) | undefined;
  const rabbit = { createConsumer: vi.fn((_opts, h) => (handler = h)) };
  const client = { _props: { timeout: 1000 }, send: vi.fn(send) };
  const logger = { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() };
  const publisher = { publish: vi.fn(async () => {}) };
  const queue = new Queue(
    rabbit as never,
    client as never,
    publisher as never,
    'app-events-queue',
    1,
    type({ command: 'string', requestId: 'string' }),
    type({ success: 'boolean', message: 'string' }),
    logger as never,
  );
  return { queue, client, handlerRef: () => handler };
};

describe('Queue late results (WAM#711)', () => {
  it('waits past an RPC timeout for the command this process is still running', async () => {
    const t = make(async () => {
      throw timeoutError();
    });
    const event = { command: 'update', requestId: 'r1' };
    let finish: () => void = () => {};
    t.queue.onEvent(
      (_data, reply) =>
        new Promise<void>((resolve) => {
          finish = () => reply({ success: true, message: 'updated' }).then(resolve);
        }),
    );

    const published = t.queue.publish(event);
    await new Promise((r) => setTimeout(r, 20));
    const consumed = t.handlerRef()?.({ body: event }, async () => {});
    finish();
    await consumed;

    await expect(published).resolves.toEqual({ success: true, message: 'updated' });
    expect(t.client.send).toHaveBeenCalledTimes(1);
  });

  it('still fails after one more timeout when the command never finishes', async () => {
    vi.useFakeTimers();
    try {
      const t = make(async () => {
        throw timeoutError();
      });
      t.queue.onEvent(() => new Promise<void>(() => {}));
      const published = t.queue.publish({ command: 'update', requestId: 'r2' });
      await vi.advanceTimersByTimeAsync(1001);
      await expect(published).resolves.toEqual({ success: false, message: 'RPC response timed out' });
    } finally {
      vi.useRealTimers();
    }
  });

  it('returns the normal reply unchanged', async () => {
    const t = make(async () => ({ body: { success: true, message: 'ok' } }));
    t.queue.onEvent(async () => {});
    await expect(t.queue.publish({ command: 'start', requestId: 'r3' })).resolves.toEqual({ success: true, message: 'ok' });
  });

  // WAM#721: a command outlasting both waits was still reported failed while it ran, so InstallAppHandler deleted the
  // new app's row and UpdateAppHandler recorded it stopped mid-update.
  it('keeps waiting past the second timeout while this process is still running the command', async () => {
    vi.useFakeTimers();
    try {
      const t = make(async () => {
        throw timeoutError();
      });
      const event = { command: 'update', requestId: 'r4' };
      let finish: () => void = () => {};
      t.queue.onEvent(
        (_data, reply) =>
          new Promise<void>((resolve) => {
            finish = () => reply({ success: true, message: 'updated late' }).then(resolve);
          }),
      );
      let settled = false;
      const published = t.queue.publish(event).finally(() => {
        settled = true;
      });
      await vi.advanceTimersByTimeAsync(10);
      const consumed = t.handlerRef()?.({ body: event }, async () => {});
      await vi.advanceTimersByTimeAsync(10 * 1000);
      expect(settled).toBe(false);
      finish();
      await consumed;
      await expect(published).resolves.toEqual({ success: true, message: 'updated late' });
    } finally {
      vi.useRealTimers();
    }
  });

  it('cancels a command whose caller gave up before it started, instead of running it behind the caller', async () => {
    vi.useFakeTimers();
    try {
      const t = make(async () => {
        throw timeoutError();
      });
      const ran = vi.fn(async () => {});
      t.queue.onEvent(ran);
      const event = { command: 'install', requestId: 'r5' };
      const published = t.queue.publish(event);
      await vi.advanceTimersByTimeAsync(1001);
      await expect(published).resolves.toEqual({ success: false, message: 'RPC response timed out' });

      const replies: unknown[] = [];
      await t.handlerRef()?.({ body: event }, async (r) => {
        replies.push(r);
      });
      expect(ran).not.toHaveBeenCalled();
      expect(replies).toEqual([{ success: false, message: 'Cancelled: the request timed out before it started.' }]);

      // Only that request is cancelled: the next one with a new id runs.
      await t.handlerRef()?.({ body: { command: 'install', requestId: 'r6' } }, async () => {});
      expect(ran).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
