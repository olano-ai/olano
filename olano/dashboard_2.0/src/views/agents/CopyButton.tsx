// A copy-to-clipboard button that confirms in place.
//
// The dashboard hand-rolls `navigator.clipboard.writeText` in ~20 places; this
// is not a refactor of those, just one shared implementation for the connector
// surfaces so the URL and the API key behave identically. Keeps the two
// details those call sites got right: the check mark reverts on a timer (a
// permanent tick reads as state, not as an acknowledgement), and a rejected
// clipboard — a non-secure origin, or a browser that refuses without a user
// gesture — says so instead of failing silently, because the user is about to
// paste something they never got.

import { useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";
import { useToast } from "../../ui/toast";

export function CopyButton({
  value,
  label,
  t,
}: {
  value: string;
  /** Already-translated accessible name, e.g. "Copy". */
  label: string;
  t: (key: string) => string;
}) {
  const toast = useToast();
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // A copy whose confirmation outlives the field (the modal closes, the
  // connector type changes) would set state on an unmounted component.
  useEffect(() => () => void (timer.current && clearTimeout(timer.current)), []);

  function copy() {
    const done = () => {
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1500);
    };
    try {
      const write = navigator.clipboard?.writeText(value);
      if (!write) throw new Error("clipboard unavailable");
      void write.then(done, () => toast.push(t("catalog.common.copyFailed"), "error"));
    } catch {
      toast.push(t("catalog.common.copyFailed"), "error");
    }
  }

  return (
    <button
      type="button"
      className="btn ghost"
      onClick={copy}
      aria-label={label}
      title={label}
      style={{ padding: 6, lineHeight: 0 }}
    >
      {copied ? <Check size={15} strokeWidth={1.75} /> : <Copy size={15} strokeWidth={1.75} />}
    </button>
  );
}
