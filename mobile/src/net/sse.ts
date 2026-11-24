/**
 * An incremental server-sent-events parser.
 *
 * The runtime's chat endpoints answer `text/event-stream` with one JSON object
 * per event (`data: {...}` then a blank line). Network reads split that text
 * anywhere, mid-line included, so the parser keeps what it has not finished
 * and only emits complete events. Comment lines (`: keep-alive`) and fields
 * other than `data` are skipped; several `data:` lines join with "\n", as the
 * SSE specification says.
 *
 * Pure: no DOM, no network, so `node --test` covers it.
 */

export class SseParser {
  private buffer = "";
  private data: string[] = [];

  /** Feeds decoded text; returns the `data` of every event it completed. */
  push(text: string): string[] {
    this.buffer += text;
    const events: string[] = [];
    let newline: number;
    while ((newline = this.buffer.search(/\r\n|\r|\n/)) !== -1) {
      const line = this.buffer.slice(0, newline);
      const breakLength = this.buffer.startsWith("\r\n", newline) ? 2 : 1;
      // A lone "\r" at the very end may be the first half of "\r\n": wait for more.
      if (breakLength === 1 && this.buffer[newline] === "\r" && newline === this.buffer.length - 1) break;
      this.buffer = this.buffer.slice(newline + breakLength);
      if (line === "") {
        if (this.data.length) events.push(this.data.join("\n"));
        this.data = [];
      } else if (line.startsWith("data:")) {
        this.data.push(line.slice(line.startsWith("data: ") ? 6 : 5));
      }
    }
    return events;
  }

  /** Whatever event the stream ended in the middle of, if any. */
  flush(): string[] {
    const tail = this.buffer;
    this.buffer = "";
    if (tail.startsWith("data:")) this.data.push(tail.slice(tail.startsWith("data: ") ? 6 : 5));
    const events = this.data.length ? [this.data.join("\n")] : [];
    this.data = [];
    return events;
  }
}

/** Parses each event's data as JSON, skipping anything that is not an object. */
export function jsonFrames<F>(events: string[]): F[] {
  const frames: F[] = [];
  for (const event of events) {
    try {
      const value: unknown = JSON.parse(event);
      if (value && typeof value === "object" && !Array.isArray(value)) frames.push(value as F);
    } catch {
      // A partial or non-JSON event (the runtime never sends one) is dropped.
    }
  }
  return frames;
}
