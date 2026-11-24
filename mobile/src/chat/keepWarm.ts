/**
 * Something costly to open (the microphone) kept open a little longer than
 * its last use, so the next use is instant: a `hold()` cancels the pending
 * release, a `releaseSoon()` schedules one, `releaseNow()` releases at once.
 * Pure (timers are injected), so `node --test` covers it.
 */

type Timer = ReturnType<typeof setTimeout>;

export class KeepWarm {
  private timer: Timer | null = null;
  private readonly release: () => void;
  private readonly delayMs: number;
  private readonly schedule: (fn: () => void, ms: number) => Timer;
  private readonly cancel: (timer: Timer) => void;

  constructor(
    release: () => void,
    delayMs: number,
    // Wrapped: a browser's setTimeout refuses to be called as a method of anything else.
    schedule: (fn: () => void, ms: number) => Timer = (fn, ms) => setTimeout(fn, ms),
    cancel: (timer: Timer) => void = (timer) => clearTimeout(timer),
  ) {
    this.release = release;
    this.delayMs = delayMs;
    this.schedule = schedule;
    this.cancel = cancel;
  }

  /** In use: no release while it is. */
  hold(): void {
    if (this.timer !== null) this.cancel(this.timer);
    this.timer = null;
  }

  /** No longer in use: released after the delay unless held again first. */
  releaseSoon(): void {
    this.hold();
    this.timer = this.schedule(() => {
      this.timer = null;
      this.release();
    }, this.delayMs);
  }

  releaseNow(): void {
    this.hold();
    this.release();
  }

  get pending(): boolean {
    return this.timer !== null;
  }
}
