export class AsyncMutex {
  resourceLocks: Map<string, Promise<void>>;

  constructor() {
    this.resourceLocks = new Map();
  }

  /** True while a command holds or waits for this resource (an app lifecycle command is really running). */
  isLocked(resourceId: string) {
    return this.resourceLocks.has(resourceId);
  }

  async acquire(resourceId: string) {
    const lastLock = this.resourceLocks.get(resourceId) || Promise.resolve();

    let releaseLock: () => void;
    const currentLock = new Promise<void>((resolve) => {
      releaseLock = resolve;
    });

    this.resourceLocks.set(resourceId, currentLock);

    await lastLock;

    return () => {
      if (this.resourceLocks.get(resourceId) === currentLock) {
        this.resourceLocks.delete(resourceId);
      }
      releaseLock();
    };
  }
}
