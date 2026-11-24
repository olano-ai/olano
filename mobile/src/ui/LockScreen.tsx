/**
 * Over everything while the app is locked (native/appLock.ts): it asks the
 * phone to unlock as soon as it shows, and keeps a button to ask again. In
 * the background, with the lock on, a plain cover stands in for the app so
 * the app switcher shows nothing.
 */

import { IonButton, IonIcon } from "@ionic/react";
import { lockClosed } from "ionicons/icons";
import { useEffect, useRef, useState } from "react";

import { lockName } from "../lock/lock.ts";
import { lockState, lockStatus, setLockPrefs, tryUnlock, type LockStatus } from "../native/appLock.ts";
import { platform } from "../native/shell.ts";
import { useStore } from "../state/store.ts";

export function LockScreen({ active }: { active: boolean }) {
  const { locked, covered } = useStore(lockState);
  const [status, setStatus] = useState<LockStatus | null>(null);
  const [note, setNote] = useState("");
  const asking = useRef(false);

  async function ask() {
    if (asking.current) return;
    asking.current = true;
    setNote("");
    const result = await tryUnlock();
    asking.current = false;
    if (result === "unavailable") {
      // The phone no longer has a passcode, so nothing can stand behind the
      // lock: turn it off rather than shutting the person out.
      setLockPrefs({ enabled: false });
      lockState.set({ locked: false, covered: false });
    } else if (result === "failed") {
      setNote("That didn't work. Try again.");
    }
  }

  useEffect(() => {
    if (!active || !locked) return;
    void lockStatus().then(setStatus);
    if (!document.hidden) void ask();
    const onVisible = () => {
      if (!document.hidden && lockState.get().locked) void ask();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [active, locked]);

  if (!active || (!locked && !covered)) return null;
  const name = status ? lockName(status.kind, platform()) : "";
  return (
    <div className={`olano-lock${locked ? "" : " cover"}`} role={locked ? "dialog" : undefined} aria-label={locked ? "Olano is locked" : undefined}>
      <IonIcon icon={lockClosed} className="olano-lock-icon" />
      {locked && (
        <>
          <h2>Olano is locked</h2>
          <IonButton onClick={() => void ask()}>{name ? `Unlock with ${name}` : "Unlock"}</IonButton>
          {note && <p>{note}</p>}
        </>
      )}
    </div>
  );
}
