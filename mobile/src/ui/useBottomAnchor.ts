/**
 * Keeps a conversation's newest message in view while things change size
 * around it: the keyboard opening or closing (the web view shrinks above it),
 * the composer growing, a reply's buttons appearing once it finishes. The
 * rules are `chat/anchor.ts`; this watches the sizes and applies them.
 *
 * `stick` is the page's own "following the newest message" flag, which its
 * scroll handler keeps; scrolling here fires that handler like any scroll.
 * The returned `settled(el)` tells that handler whether a scroll can be read
 * as the person's position (`scrollIsSettled` in chat/anchor.ts): a scroll
 * that lands between a resize and this hook's answer to it must not drop the
 * flag, or the keyboard keeps the newest reply behind the composer.
 */

import { useCallback, useEffect, useRef } from "react";
import type { RefObject } from "react";

import { anchoredScrollTop, distanceFromBottom, scrollIsSettled } from "../chat/anchor.ts";

export function useBottomAnchor(
  content: RefObject<HTMLIonContentElement | null>,
  messages: RefObject<HTMLElement | null>,
  stick: { current: boolean },
): (el: HTMLElement) => boolean {
  // The box height this hook last answered for; 0 until it starts.
  const seen = useRef(0);
  useEffect(() => {
    let el: HTMLElement | null = null;
    let observer: ResizeObserver | null = null;
    let cancelled = false;
    // Measured before each change: a resize has already happened when the
    // observer hears of it.
    let fromBottom = 0;
    const measure = () => {
      if (!el || !el.clientHeight || !scrollIsSettled(el.clientHeight, seen.current)) return;
      fromBottom = distanceFromBottom(el);
    };
    void content.current?.getScrollElement().then((scrollEl) => {
      if (cancelled || typeof ResizeObserver === "undefined") return;
      el = scrollEl;
      seen.current = el.clientHeight;
      measure();
      el.addEventListener("scroll", measure, { passive: true });
      observer = new ResizeObserver((entries) => {
        // A page slid away or under another tab reports an empty box.
        if (!el || !el.clientHeight) return;
        // The scroll element's own box moves with the keyboard and, through
        // Ionic's bottom padding under a fullscreen content, with the composer.
        const change = entries.some((entry) => entry.target === el) ? "viewport" : "messages";
        const top = anchoredScrollTop(el, change, stick.current, fromBottom);
        if (top !== null) el.scrollTop = top;
        seen.current = el.clientHeight;
        measure();
      });
      // The border box, not the default content box: `seen` is only right if
      // every change of clientHeight reaches this observer, and under a
      // fullscreen content some do not change the content box. The composer
      // growing moves the box first, then Ionic moves its bottom edge and
      // its bottom padding by the same amount (`--offset-bottom`), which
      // restores clientHeight and leaves the content box as it was; seen
      // through the content box, `seen` stayed at the in-between height and
      // every later scroll read as unsettled, so scrolling up to read never
      // registered.
      observer.observe(el, { box: "border-box" });
      if (messages.current) observer.observe(messages.current);
    });
    return () => {
      cancelled = true;
      observer?.disconnect();
      el?.removeEventListener("scroll", measure);
      seen.current = 0;
    };
  }, [content, messages, stick]);
  return useCallback((el: HTMLElement) => scrollIsSettled(el.clientHeight, seen.current), []);
}
