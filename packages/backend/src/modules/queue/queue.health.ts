import { Injectable } from '@nestjs/common';
import { type HealthIndicatorResult, HealthIndicatorService } from '@nestjs/terminus';
import { QueueFactory } from './queue.factory';

@Injectable()
export class QueueHealthIndicator {
  constructor(
    private readonly queueFactory: QueueFactory,
    private readonly healthIndicatorService: HealthIndicatorService,
  ) {}

  async isHealthy(key: string): Promise<HealthIndicatorResult> {
    const indicator = this.healthIndicatorService.check(key);
    const isHealthy = this.queueFactory.isReady();

    if (!isHealthy) {
      return indicator.down();
    }

    return indicator.up();
  }
}
