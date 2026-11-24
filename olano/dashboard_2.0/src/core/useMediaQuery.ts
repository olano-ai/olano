import { useEffect, useState } from "react";

// Reactive matchMedia. The dashboard's single layout breakpoint lives here so
// JS (Layout's off-canvas nav) and CSS (globals.css media queries) agree.
export const MOBILE_BREAKPOINT_PX = 900;
export const MOBILE_QUERY = `(max-width: ${MOBILE_BREAKPOINT_PX}px)`;

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const onChange = () => setMatches(mq.matches);
    mq.addEventListener("change", onChange);
    setMatches(mq.matches);
    return () => mq.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}
