/**
 * A group chat's picture: its first two members, one tucked behind the other,
 * as a messaging app draws a group.
 */

import type { Agent } from "../net/types.ts";
import { Avatar } from "./Avatar.tsx";

export function GroupAvatar({ members, agents, size = 50 }: { members: readonly string[]; agents: readonly Agent[]; size?: number }) {
  const faces = members.slice(0, 2).map((name) => agents.find((a) => a.name === name) ?? { name });
  const small = Math.round(size * 0.68);
  if (faces.length < 2) {
    return faces[0] ? <Avatar agent={faces[0]} size={size} /> : null;
  }
  return (
    <span className="olano-group-avatar" style={{ width: size, height: size }} aria-hidden="true">
      <span className="olano-group-avatar-back">
        <Avatar agent={faces[1]} size={small} />
      </span>
      <span className="olano-group-avatar-front">
        <Avatar agent={faces[0]} size={small} />
      </span>
    </span>
  );
}
