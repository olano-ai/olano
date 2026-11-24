/**
 * An agent's picture. Avatars need the deployment's session cookie, so they
 * are fetched by native code and kept in memory as data: URLs for the rest of
 * the session, and on the phone between launches when their address is
 * stamped (ui/avatarCache.ts); until one arrives (or when an agent has none)
 * the agent's emoji or initial is drawn on a colour derived from its name.
 */

import { useEffect, useState } from "react";

import type { Agent } from "../net/types.ts";
import { session } from "../state/session.ts";
import { defaultCacheStore } from "../state/persisted.ts";
import { readAvatar, writeAvatar } from "./avatarCache.ts";

const cache = new Map<string, Promise<string | null>>();

export function loadImage(path: string): Promise<string | null> {
  let pending = cache.get(path);
  if (!pending) {
    const { api, origin } = session.get();
    const kept = origin ? readAvatar(defaultCacheStore(), origin, path) : null;
    if (kept) {
      pending = Promise.resolve(kept);
    } else {
      pending = api
        ? api
            .image(path)
            .then((value) => {
              if (value && origin) writeAvatar(defaultCacheStore(), origin, path, value);
              return value;
            })
            .catch(() => null)
        : Promise.resolve(null);
    }
    cache.set(path, pending);
    // A failure is not remembered: the next screen that shows it tries again.
    pending.then((value) => {
      if (value === null) cache.delete(path);
    });
  }
  return pending;
}

export function useAuthedImage(path: string | null | undefined): string | null {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setSrc(null);
    if (path) void loadImage(path).then((value) => alive && setSrc(value));
    return () => {
      alive = false;
    };
  }, [path]);
  return src;
}

function hue(name: string): number {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return h;
}

export function displayName(agent: Pick<Agent, "name" | "display_name">): string {
  return (agent.display_name || agent.name || "").trim() || agent.name;
}

export function Avatar({
  agent,
  size = 44,
  online,
}: {
  agent: Pick<Agent, "name" | "display_name" | "emoji" | "avatar_url" | "color">;
  size?: number;
  online?: boolean;
}) {
  const src = useAuthedImage(agent.avatar_url);
  const label = displayName(agent);
  const fallback = agent.emoji?.trim() || label.slice(0, 1).toUpperCase();
  const background = agent.color || `hsl(${hue(agent.name)} 55% 46%)`;
  return (
    <span className="olano-avatar" style={{ width: size, height: size }} aria-hidden="true">
      {src ? (
        <img src={src} alt="" width={size} height={size} />
      ) : (
        <span className="olano-avatar-fallback" style={{ background, fontSize: size * 0.42 }}>
          {fallback}
        </span>
      )}
      {online !== undefined && <span className={online ? "olano-presence on" : "olano-presence"} />}
    </span>
  );
}
