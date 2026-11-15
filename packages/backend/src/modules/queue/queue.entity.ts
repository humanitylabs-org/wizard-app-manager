import type { LoggerService } from '@/core/logger/logger.service';
import * as Sentry from '@sentry/nestjs';
import { AMQPConnectionError, AMQPError, type Connection, type RPCClient } from 'rabbitmq-client';
import { type } from 'arktype';
import { scheduleCron, validateCronPattern } from './cron-scheduler';
import type { EventPublisher } from './event.publisher';

export type ArkTypeSchema<TOut = unknown, TIn = unknown> = {
  (data: TIn): TOut | type.errors;
  infer: TOut;
  inferIn?: TIn;
};

type Infer<T extends { infer: unknown }> = T['infer'];
type InferIn<T extends { infer: unknown; inferIn?: unknown }> = T extends { inferIn: infer I } ? I : T['infer'];

export class Queue<T extends ArkTypeSchema, R extends ArkTypeSchema<{ success: boolean; message: string }>> {
  constructor(
    private rabbit: Connection,
    private rpcClient: RPCClient,
    private publisher: EventPublisher,
    private queueName: string,
    private workers: number,
    private eventSchema: T,
    private resultSchema: R,
    private logger: LoggerService,
    private long: { client: RPCClient; commands: readonly string[] } = { client: rpcClient, commands: [] },
  ) {}

  /** Commands this process consumes, by requestId: a publisher whose RPC timed out waits here for the real result. */
  private lateResults = new Map<string, (response: { success: boolean; message: string }) => void>();
  private consuming = false;
  /** requestIds whose command this process's consumer is running right now: their publisher waits for the real result. */
  private running = new Set<string>();
  /**
   * requestIds whose publisher gave up before any consumer here started them: cancelled, never run late (WAM#721).
   * In memory is enough: RPCClient.send publishes every request with an AMQP expiration of one timeout, so the broker
   * never delivers it after that, also after a restart (requeued messages keep their expiry). The publisher gives up
   * only after two timeouts, so this map covers just the messages already prefetched into this process.
   */
  private abandoned = new Map<string, number>();

  /** The RPC client for this event: long-running commands get the client with the long timeout (WAM#711). */
  public clientFor(event: unknown): RPCClient {
    const command = (event as { command?: unknown } | null)?.command;
    return typeof command === 'string' && this.long.commands.includes(command) ? this.long.client : this.rpcClient;
  }

  public onEvent(callback: (data: Infer<T> & { eventId: string }, reply: (response: InferIn<R>) => Promise<void>) => Promise<void>) {
    try {
      this.rabbit.createConsumer(
        {
          queue: this.queueName,
          concurrency: this.workers,
          queueOptions: { autoDelete: false, durable: true },
        },
        async (req, rawReply) => {
          let rpcSuccess = false;
          let rpcResultMessage = '';
          let replied: { success: boolean; message: string } | undefined;
          const requestId = typeof req.body?.requestId === 'string' ? (req.body.requestId as string) : undefined;
          if (requestId && this.abandoned.delete(requestId)) {
            // The caller already reported this command as failed; running it now would act behind its back (WAM#721).
            this.logger.warn(`${this.queueName}: skipping ${requestId}; its caller gave up before it started`);
            await rawReply({ success: false, message: 'Cancelled: the request timed out before it started.' });
            return;
          }
          if (requestId) this.running.add(requestId);
          const reply = async (response: InferIn<R>) => {
            replied = response as { success: boolean; message: string };
            await rawReply(response);
          };

          try {
            await callback(req.body, reply);
            rpcSuccess = true;
            rpcResultMessage = 'RPC processed successfully.';
          } catch (error) {
            this.logger.error('Error in consumer callback:', error);
            await reply({ success: false, message: (error as Error)?.message } as InferIn<R>);
            rpcSuccess = false;
            rpcResultMessage = error instanceof Error ? error.message : String(error);
          } finally {
            if (requestId) this.running.delete(requestId);
            const eventToPublish = {
              queueName: this.queueName,
              requestData: req.body,
              rpcStatus: rpcSuccess ? 'success' : 'failure',
              rpcMessage: rpcResultMessage,
              requestId: req.body.requestId,
              timestamp: new Date().toISOString(),
            };

            const late = this.lateResults.get(req.body?.requestId);
            if (late) late(replied ?? { success: rpcSuccess, message: rpcResultMessage });

            const routingKey = `rpc.${rpcSuccess ? 'processed' : 'error'}.${this.queueName}`;
            await this.publisher.publish(routingKey, eventToPublish);
          }
        },
      );
      this.consuming = true;
    } catch (error) {
      this.logger.error(`Failed to create consumer for queue ${this.queueName}:`, error);
      Sentry.captureException(error, { tags: { queueName: this.queueName, action: 'onEvent' } });
      throw error;
    }
  }

  async publish(event: unknown): Promise<{ success: boolean; message: string } | Infer<R>> {
    try {
      const eventData = this.eventSchema(event as never);
      if (eventData instanceof type.errors) {
        throw new Error(`Invalid event data: ${eventData.summary}`);
      }

      const client = this.clientFor(eventData);
      const body = await this.sendAndWaitLate(client, eventData);
      const response = this.resultSchema(body as never);
      if (!(response instanceof type.errors)) {
        return response;
      }

      throw new Error(`Invalid response schema: ${response.summary}`);
    } catch (err) {
      if (err instanceof AMQPConnectionError) {
        this.logger.error('Connection to the queue was lost. Try restarting your instance before retrying.');
      }

      if (err instanceof AMQPError) {
        if (err.code === 'RPC_TIMEOUT') {
          this.logger.error('The queue timed out while processing the request. Try restarting your instance before retrying.');
        }
        return { success: false, message: err.message };
      }

      Sentry.captureException(err, { tags: { queueName: this.queueName } });
      return { success: false, message: String(err) };
    }
  }

  /**
   * An RPC timeout does not stop the command: the consumer keeps running it. When this process consumes the queue
   * itself, wait for the real result instead of reporting a failure while the command still runs, so the caller
   * records the true outcome (WAM#711: a big update was left `stopped` on its old version).
   * A command that has not started yet gets one more timeout. Once this process's consumer is running it, the
   * publisher waits for its real result however long it takes (WAM#721). A command still not started when the
   * extra wait ends is cancelled here, so the failure the caller records stays true.
   */
  private async sendAndWaitLate(client: RPCClient, eventData: unknown): Promise<unknown> {
    const requestId = (eventData as { requestId?: unknown } | null)?.requestId;
    if (!this.consuming || typeof requestId !== 'string') {
      return (await client.send(this.queueName, eventData)).body;
    }

    let resolveLate: (response: { success: boolean; message: string }) => void = () => {};
    let finished: { success: boolean; message: string } | undefined;
    const late = new Promise<{ success: boolean; message: string }>((resolve) => {
      resolveLate = (response) => {
        finished = response;
        resolve(response);
      };
    });
    this.lateResults.set(requestId, resolveLate);

    try {
      return (await client.send(this.queueName, eventData)).body;
    } catch (err) {
      if (!(err instanceof AMQPError) || err.code !== 'RPC_TIMEOUT') throw err;
      this.logger.warn(`${this.queueName}: no reply in time for ${requestId}; waiting for the command to finish`);
      let timer: ReturnType<typeof setTimeout> | undefined;
      const giveUp = new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(err), this.lateWaitMs(client));
      });
      try {
        return await Promise.race([late, giveUp]);
      } catch (waitErr) {
        if (finished) return finished;
        if (this.running.has(requestId)) {
          this.logger.warn(`${this.queueName}: ${requestId} is still running; waiting for its real result`);
          return await late;
        }
        this.abandon(requestId);
        throw waitErr;
      } finally {
        clearTimeout(timer);
      }
    } finally {
      this.lateResults.delete(requestId);
    }
  }

  private abandon(requestId: string) {
    const now = Date.now();
    for (const [id, at] of this.abandoned) {
      if (now - at > 24 * 60 * 60_000) this.abandoned.delete(id);
    }
    this.abandoned.set(requestId, now);
  }

  private lateWaitMs(client: RPCClient) {
    const timeout = (client as unknown as { _props?: { timeout?: number } })._props?.timeout;
    return typeof timeout === 'number' && timeout > 0 ? timeout : 30_000;
  }

  public publishRepeatable(data: unknown, cronPattern: string) {
    if (!validateCronPattern(cronPattern)) {
      throw new Error('Invalid cron pattern');
    }

    const eventData = this.eventSchema(data as never);
    if (eventData instanceof type.errors) {
      throw new Error(`Invalid event data: ${eventData.summary}`);
    }

    scheduleCron(cronPattern, async () => {
      try {
        await this.clientFor(eventData).send(this.queueName, eventData);
      } catch (e) {
        Sentry.captureException(e, { tags: { queueName: this.queueName } });
        this.logger.error('Error in cron job:', e);
      }
    });
  }
}
