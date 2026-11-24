/**
 * The agent's browser as a small floating window over the conversation, so
 * the person sees what the agent is doing while reading its replies: the live
 * picture and who has control. Drag it to any corner; tap it for the full
 * view; the x hides it until the agent opens a new browser or asks for help
 * again (the globe in the header brings it back sooner).
 */

import { IonIcon } from "@ionic/react";
import { close } from "ionicons/icons";
import { useEffect, useRef, useState } from "react";

import type { LiveBrowser } from "../../../browser/live.ts";
import { statusLine } from "../../../browser/model.ts";
import { viewSizeFor } from "../../../browser/viewSize.ts";
import { haptic } from "../../../native/haptics.ts";
import { useStore } from "../../../state/store.ts";

type Corner = "tl" | "tr" | "bl" | "br";
const CORNER_KEY = "olano.browser.corner";

function savedCorner(): Corner {
  try {
    const value = localStorage.getItem(CORNER_KEY);
    return value === "tl" || value === "tr" || value === "bl" || value === "br" ? value : "tr";
  } catch {
    return "tr";
  }
}

export function BrowserPip({
  browser,
  attention,
  approvalPending,
  onOpen,
  onHide,
}: {
  browser: LiveBrowser;
  attention: string | null;
  approvalPending: boolean;
  onOpen: () => void;
  onHide: () => void;
}) {
  const view = useStore(browser.store);
  const [corner, setCorner] = useState<Corner>(savedCorner);
  const [drag, setDrag] = useState<{ x: number; y: number } | null>(null);
  const origin = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const element = useRef<HTMLDivElement>(null);

  useEffect(() => {
    browser.acquire();
    return () => browser.release();
  }, [browser]);

  const line = statusLine(view.state?.controller, view.state?.attention ?? attention, approvalPending);

  function settle(clientX: number, clientY: number) {
    const parent = element.current?.parentElement?.getBoundingClientRect();
    if (!parent) return;
    const left = clientX - parent.left < parent.width / 2;
    const top = clientY - parent.top < parent.height / 2;
    const next: Corner = `${top ? "t" : "b"}${left ? "l" : "r"}` as Corner;
    setCorner(next);
    try {
      localStorage.setItem(CORNER_KEY, next);
    } catch {
      // Not remembered; the window still moves.
    }
  }

  return (
    <div
      ref={element}
      className={`olano-pip ${corner}${drag ? " dragging" : ""} ${line.tone}`}
      style={drag ? { transform: `translate(${drag.x}px, ${drag.y}px)` } : undefined}
      role="button"
      tabIndex={0}
      aria-label={`The agent's browser: ${line.text}. Open it`}
      onPointerDown={(e) => {
        e.currentTarget.setPointerCapture?.(e.pointerId);
        origin.current = { x: e.clientX, y: e.clientY, moved: false };
      }}
      onPointerMove={(e) => {
        const o = origin.current;
        if (!o) return;
        const dx = e.clientX - o.x;
        const dy = e.clientY - o.y;
        if (!o.moved && Math.hypot(dx, dy) < 8) return;
        o.moved = true;
        setDrag({ x: dx, y: dy });
      }}
      onPointerUp={(e) => {
        const o = origin.current;
        origin.current = null;
        setDrag(null);
        if (o?.moved) {
          settle(e.clientX, e.clientY);
          void haptic("light");
        } else {
          onOpen();
        }
      }}
      onPointerCancel={() => {
        origin.current = null;
        setDrag(null);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") onOpen();
      }}
    >
      <div className="olano-pip-picture">
        {view.frame ? (
          <img
            src={view.frame}
            alt=""
            draggable={false}
            onLoad={(e) => {
              // A small window needs small frames: the deployment sends no
              // more pixels than this picture draws.
              const box = e.currentTarget.getBoundingClientRect();
              browser.setViewSize(viewSizeFor({ w: box.width, h: box.height }, window.devicePixelRatio));
              browser.frameShown();
            }}
            onError={() => browser.frameShown()}
          />
        ) : (
          <span className="olano-pip-wait" />
        )}
      </div>
      <div className="olano-pip-status">
        <span className="dot" aria-hidden="true" />
        <span>{line.text}</span>
      </div>
      <button
        type="button"
        className="olano-pip-close"
        aria-label="Hide the browser window"
        onPointerDown={(e) => e.stopPropagation()}
        onPointerUp={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.stopPropagation();
          onHide();
        }}
      >
        <IonIcon icon={close} />
      </button>
    </div>
  );
}
