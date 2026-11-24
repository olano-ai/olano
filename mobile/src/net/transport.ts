/**
 * How the app reaches a deployment: one interface, two implementations.
 *
 * - **native** (the app): the OlanoHttp plugin (OlanoHttpPlugin.swift / .java).
 *   The screens run on the app's own origin, so the browser's fetch() cannot
 *   carry the deployment's session cookie or read its responses; native code
 *   can, and it streams.
 * - **web** (`npm run dev`, the end-to-end tests): fetch() against the page's
 *   own origin, which Vite proxies to a runtime. The deployment origin in a
 *   URL is replaced by the page's, so the rest of the app never knows.
 *
 * A stream is the chat's server-sent events: `onText` receives decoded text in
 * order and `done` settles when the response ends, fails or is cancelled.
 *
 * A socket is a WebSocket to the deployment's API (the live browser view):
 * text and binary messages in, text out. Native code opens it with the
 * session cookie; in a browser it goes through the dev server's proxy.
 */

import { registerPlugin, type PluginListenerHandle } from "@capacitor/core";

import { isNative } from "../native/shell.ts";

export interface HttpResponse {
  status: number;
  /** Lower-cased header names. */
  headers: Record<string, string>;
  /** Text, or base64 when `responseType` was "base64". */
  body: string;
  url: string;
}

export interface RequestOptions {
  method?: string;
  headers?: Record<string, string>;
  /** Text, or base64 of raw bytes when `bodyEncoding` is "base64" (a file upload). */
  body?: string;
  bodyEncoding?: "text" | "base64";
  responseType?: "text" | "base64";
  timeoutMs?: number;
}

export interface StreamEnd {
  error?: string;
  cancelled?: boolean;
}

export interface OpenStream {
  status: number;
  headers: Record<string, string>;
  cancel(): void;
  done: Promise<StreamEnd>;
}

export interface SocketHandlers {
  onOpen(): void;
  onText(text: string): void;
  onBinary(bytes: Uint8Array): void;
  /** Called once, however it ended. `code` 1000 is a normal close. */
  onClose(end: { code: number; reason: string; error?: string }): void;
}

export interface OpenSocket {
  send(text: string): void;
  close(): void;
}

export interface Transport {
  request(url: string, options?: RequestOptions): Promise<HttpResponse>;
  stream(url: string, options: RequestOptions, onText: (text: string) => void): Promise<OpenStream>;
  socket(url: string, handlers: SocketHandlers): OpenSocket;
}

/** A request the transport refused or could not complete (no HTTP status). */
export class NetworkError extends Error {
  readonly code: string;
  constructor(message: string, code = "NETWORK") {
    super(message);
    this.name = "NetworkError";
    this.code = code;
  }
}

// ── native ───────────────────────────────────────────────────────────────────

interface OlanoHttpPlugin {
  request(options: {
    url: string;
    method?: string;
    headers?: Record<string, string>;
    body?: string;
    bodyEncoding?: "text" | "base64";
    responseType?: "text" | "base64";
    timeout?: number;
  }): Promise<HttpResponse>;
  stream(options: {
    id: string;
    url: string;
    method?: string;
    headers?: Record<string, string>;
    body?: string;
  }): Promise<{ status: number; headers: Record<string, string> }>;
  cancel(options: { id: string }): Promise<void>;
  clearCookies(): Promise<void>;
  socketOpen(options: { id: string; url: string; headers?: Record<string, string> }): Promise<void>;
  socketSend(options: { id: string; text: string }): Promise<void>;
  socketClose(options: { id: string }): Promise<void>;
  addListener(event: "chunk", fn: (e: { id: string; data: string }) => void): Promise<PluginListenerHandle>;
  addListener(event: "end", fn: (e: { id: string; error?: string; cancelled?: boolean }) => void): Promise<PluginListenerHandle>;
  addListener(event: "socketOpen", fn: (e: { id: string }) => void): Promise<PluginListenerHandle>;
  addListener(event: "socketMessage", fn: (e: { id: string; binary: boolean; data: string }) => void): Promise<PluginListenerHandle>;
  addListener(event: "socketClose", fn: (e: { id: string; code: number; reason?: string; error?: string }) => void): Promise<PluginListenerHandle>;
}

const OlanoHttp = registerPlugin<OlanoHttpPlugin>("OlanoHttp");

function decodeBase64(data: string): Uint8Array {
  const binary = atob(data);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

interface NativeSink {
  decoder: TextDecoder;
  onText: (text: string) => void;
  finish: (end: StreamEnd) => void;
}

class NativeTransport implements Transport {
  private sinks = new Map<string, NativeSink>();
  private sockets = new Map<string, SocketHandlers>();
  private listening: Promise<void> | null = null;
  private socketListening: Promise<void> | null = null;
  private nextId = 0;

  private listenSockets(): Promise<void> {
    if (!this.socketListening) {
      this.socketListening = Promise.all([
        OlanoHttp.addListener("socketOpen", ({ id }) => this.sockets.get(id)?.onOpen()),
        OlanoHttp.addListener("socketMessage", ({ id, binary, data }) => {
          const handlers = this.sockets.get(id);
          if (!handlers) return;
          if (binary) handlers.onBinary(decodeBase64(data));
          else handlers.onText(data);
        }),
        OlanoHttp.addListener("socketClose", ({ id, code, reason, error }) => {
          const handlers = this.sockets.get(id);
          if (!handlers) return;
          this.sockets.delete(id);
          handlers.onClose({ code: code || 1006, reason: reason || "", error });
        }),
      ]).then(() => undefined);
    }
    return this.socketListening;
  }

  socket(url: string, handlers: SocketHandlers): OpenSocket {
    const id = `w${Date.now().toString(36)}${(this.nextId++).toString(36)}`;
    let closed = false;
    const fail = (error: unknown) => {
      if (!this.sockets.delete(id)) return;
      handlers.onClose({ code: 1006, reason: "", error: toNetworkError(error).message });
    };
    this.sockets.set(id, handlers);
    void this.listenSockets()
      .then(() => {
        if (closed) {
          this.sockets.delete(id);
          return;
        }
        return OlanoHttp.socketOpen({ id, url });
      })
      .catch(fail);
    return {
      send: (text) => {
        OlanoHttp.socketSend({ id, text }).catch(() => undefined);
      },
      close: () => {
        closed = true;
        OlanoHttp.socketClose({ id }).catch(() => undefined);
      },
    };
  }

  private listen(): Promise<void> {
    if (!this.listening) {
      this.listening = Promise.all([
        OlanoHttp.addListener("chunk", ({ id, data }) => {
          const sink = this.sinks.get(id);
          if (!sink) return;
          const text = sink.decoder.decode(decodeBase64(data), { stream: true });
          if (text) sink.onText(text);
        }),
        OlanoHttp.addListener("end", ({ id, error, cancelled }) => {
          const sink = this.sinks.get(id);
          if (!sink) return;
          this.sinks.delete(id);
          const tail = sink.decoder.decode();
          if (tail) sink.onText(tail);
          sink.finish({ error, cancelled });
        }),
      ]).then(() => undefined);
    }
    return this.listening;
  }

  async request(url: string, options: RequestOptions = {}): Promise<HttpResponse> {
    try {
      return await OlanoHttp.request({
        url,
        method: options.method,
        headers: options.headers,
        body: options.body,
        bodyEncoding: options.bodyEncoding,
        responseType: options.responseType,
        timeout: options.timeoutMs,
      });
    } catch (error) {
      throw toNetworkError(error);
    }
  }

  async stream(url: string, options: RequestOptions, onText: (text: string) => void): Promise<OpenStream> {
    await this.listen();
    const id = `s${Date.now().toString(36)}${(this.nextId++).toString(36)}`;
    let finish!: (end: StreamEnd) => void;
    const done = new Promise<StreamEnd>((resolve) => (finish = resolve));
    // Registered before the call: the first chunk can arrive right behind the answer.
    this.sinks.set(id, { decoder: new TextDecoder(), onText, finish });
    let head: { status: number; headers: Record<string, string> };
    try {
      head = await OlanoHttp.stream({ id, url, method: options.method, headers: options.headers, body: options.body });
    } catch (error) {
      this.sinks.delete(id);
      throw toNetworkError(error);
    }
    return {
      status: head.status,
      headers: head.headers,
      cancel: () => {
        OlanoHttp.cancel({ id }).catch(() => undefined);
      },
      done,
    };
  }
}

function toNetworkError(error: unknown): NetworkError {
  const code = (error as { code?: string } | null)?.code ?? "NETWORK";
  const message = (error as { message?: string } | null)?.message || "Could not reach the deployment.";
  return new NetworkError(message, code);
}

/** Forgets the deployment's cookies on this device (sign out, switch deployment). */
export async function clearNativeCookies(): Promise<void> {
  if (isNative()) await OlanoHttp.clearCookies().catch(() => undefined);
}

// ── web ──────────────────────────────────────────────────────────────────────

/** The page's own origin stands in for the deployment's (Vite proxies /api). */
function localUrl(url: string): string {
  try {
    const parsed = new URL(url);
    return parsed.pathname + parsed.search;
  } catch {
    return url;
  }
}

function headersOf(response: Response): Record<string, string> {
  const out: Record<string, string> = {};
  response.headers.forEach((value, key) => (out[key.toLowerCase()] = value));
  return out;
}

class WebTransport implements Transport {
  socket(url: string, handlers: SocketHandlers): OpenSocket {
    return webSocket(url, handlers);
  }

  async request(url: string, options: RequestOptions = {}): Promise<HttpResponse> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? 30000);
    try {
      const response = await fetch(localUrl(url), {
        method: options.method ?? "GET",
        headers: options.headers,
        body: options.body !== undefined && options.bodyEncoding === "base64" ? new Blob([decodeBase64(options.body) as BlobPart]) : options.body,
        credentials: "include",
        redirect: "manual",
        signal: controller.signal,
      });
      let body: string;
      if (options.responseType === "base64") {
        const bytes = new Uint8Array(await response.arrayBuffer());
        let binary = "";
        for (let i = 0; i < bytes.length; i += 0x8000) {
          binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
        }
        body = btoa(binary);
      } else {
        body = await response.text();
      }
      return { status: response.status, headers: headersOf(response), body, url };
    } catch (error) {
      throw new NetworkError(controller.signal.aborted ? "The deployment took too long to answer." : "Could not reach the deployment.");
    } finally {
      clearTimeout(timer);
    }
  }

  async stream(url: string, options: RequestOptions, onText: (text: string) => void): Promise<OpenStream> {
    const controller = new AbortController();
    let response: Response;
    try {
      response = await fetch(localUrl(url), {
        method: options.method ?? "GET",
        headers: options.headers,
        body: options.body,
        credentials: "include",
        signal: controller.signal,
      });
    } catch {
      throw new NetworkError("Could not reach the deployment.");
    }
    const reader = response.body?.getReader();
    const decoder = new TextDecoder();
    const done = (async (): Promise<StreamEnd> => {
      if (!reader) return {};
      try {
        for (;;) {
          const { value, done: finished } = await reader.read();
          if (finished) break;
          const text = decoder.decode(value, { stream: true });
          if (text) onText(text);
        }
        const tail = decoder.decode();
        if (tail) onText(tail);
        return {};
      } catch (error) {
        if (controller.signal.aborted) return { cancelled: true };
        return { error: (error as Error)?.message || "The connection was lost." };
      }
    })();
    return {
      status: response.status,
      headers: headersOf(response),
      cancel: () => controller.abort(),
      done,
    };
  }
}

/** A browser's own WebSocket, on the page's origin (the dev server proxies /api). */
function webSocket(url: string, handlers: SocketHandlers): OpenSocket {
  const target = new URL(localUrl(url), location.href);
  target.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  let ended = false;
  const end = (event: { code: number; reason: string; error?: string }) => {
    if (ended) return;
    ended = true;
    handlers.onClose(event);
  };
  let ws: WebSocket;
  try {
    ws = new WebSocket(target.toString());
  } catch (error) {
    queueMicrotask(() => end({ code: 1006, reason: "", error: (error as Error)?.message || "Could not connect." }));
    return { send: () => undefined, close: () => undefined };
  }
  ws.binaryType = "arraybuffer";
  ws.onopen = () => handlers.onOpen();
  ws.onmessage = (event) => {
    if (typeof event.data === "string") handlers.onText(event.data);
    else if (event.data instanceof ArrayBuffer) handlers.onBinary(new Uint8Array(event.data));
  };
  ws.onclose = (event) => end({ code: event.code, reason: event.reason });
  ws.onerror = () => undefined;
  return {
    send: (text) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(text);
    },
    close: () => {
      if (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN) ws.close(1000);
    },
  };
}

let transport: Transport | null = null;

export function getTransport(): Transport {
  transport ??= isNative() ? new NativeTransport() : new WebTransport();
  return transport;
}

/** Tests swap the transport for a fake. */
export function setTransport(next: Transport | null): void {
  transport = next;
}
