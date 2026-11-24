/**
 * Frames reach the picture one at a time. A phone's web view decodes a frame
 * on the thread that also handles the person's taps, and the deployment can
 * send several a second: the next one waits until the picture has drawn the
 * last (or a moment has passed, in case it never says so), and of the frames
 * that arrived while it waited only the newest is shown. Nothing but the
 * newest picture is ever wanted.
 */

type Timer = ReturnType<typeof setTimeout>;

export class FramePacer<T> {
  private busy = false;
  private held: T | null = null;
  private timer: Timer | null = null;
  private readonly show: (frame: T) => void;
  private readonly fallbackMs: number;
  private readonly schedule: (fn: () => void, ms: number) => Timer;
  private readonly cancel: (timer: Timer) => void;

  constructor(
    show: (frame: T) => void,
    fallbackMs = 400,
    // Wrapped: a browser's setTimeout refuses to be called as a method of anything else.
    schedule: (fn: () => void, ms: number) => Timer = (fn, ms) => setTimeout(fn, ms),
    cancel: (timer: Timer) => void = (timer) => clearTimeout(timer),
  ) {
    this.show = show;
    this.fallbackMs = fallbackMs;
    this.schedule = schedule;
    this.cancel = cancel;
  }

  /** A frame arrived: shown now, or held (replacing any held one) until the picture is free. */
  offer(frame: T): void {
    if (this.busy) {
      this.held = frame;
      return;
    }
    this.present(frame);
  }

  /** The picture has drawn the frame it was given: the newest waiting one may come. */
  shown(): void {
    if (!this.busy) return;
    this.busy = false;
    if (this.timer) this.cancel(this.timer);
    this.timer = null;
    const next = this.held;
    this.held = null;
    if (next !== null) this.present(next);
  }

  /** Forgets everything (the socket closed). */
  reset(): void {
    this.busy = false;
    this.held = null;
    if (this.timer) this.cancel(this.timer);
    this.timer = null;
  }

  private present(frame: T): void {
    this.busy = true;
    this.timer = this.schedule(() => {
      this.timer = null;
      this.shown();
    }, this.fallbackMs);
    this.show(frame);
  }
}
