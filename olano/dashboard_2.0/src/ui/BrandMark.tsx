// Olano brand mark, the app badge: the mesh "O" (sixteen nodes on two octagon
// rings) in white on a rounded square filled with the brand colour. At 16-18 px
// the full 48-edge mesh blurs, so this is the favicon's small style
// (mobile/scripts/make_icons.py, SMALL): a heavy outer octagon for the
// silhouette, a light inner ring and the zig-zag between the rings for the
// mesh, and no node dots (the lines meet in rounded joins). The square uses
// currentColor so the sidebar tints it with the accent token; the favicon's
// white tile is for the browser tab only.
const OUTER = [
  [7.98, 2.29],
  [16.02, 2.29],
  [21.71, 7.98],
  [21.71, 16.02],
  [16.02, 21.71],
  [7.98, 21.71],
  [2.29, 16.02],
  [2.29, 7.98],
] as const;
const INNER = [
  [10.07, 6.5],
  [13.93, 6.5],
  [16.66, 9.72],
  [16.66, 14.28],
  [13.93, 17.5],
  [10.07, 17.5],
  [7.34, 14.28],
  [7.34, 9.72],
] as const;
const pathThrough = (points: readonly (readonly [number, number])[]) =>
  `M${points.map(([x, y]) => `${x} ${y}`).join("L")}Z`;
// Outer node i to inner nodes i - 1 and i + 1.
const ZIGZAG = OUTER.map(([x, y], i) => {
  const [ax, ay] = INNER[(i + 7) % 8];
  const [bx, by] = INNER[(i + 1) % 8];
  return `M${ax} ${ay}L${x} ${y}L${bx} ${by}`;
}).join("");

export function BrandIcon({ size = 18 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <rect width="24" height="24" rx="5" fill="currentColor" />
      <g stroke="#fff" strokeLinecap="round" strokeLinejoin="round">
        <path d={pathThrough(OUTER)} strokeWidth={1.55} />
        <path d={pathThrough(INNER) + ZIGZAG} strokeWidth={0.77} />
      </g>
    </svg>
  );
}

// The olano.ai wordmark — "olano" in the current text colour with ".ai" in the
// accent, set in Poppins to match the logo. Used both as the sidebar brand label
// and, in place of the duplicated page title, on the left of each view's topbar.
export function BrandWordmark({ className = "" }: { className?: string }) {
  return (
    <strong className={`brand-wordmark${className ? ` ${className}` : ""}`}>
      olano<span className="brand-wordmark-tld">.ai</span>
    </strong>
  );
}
