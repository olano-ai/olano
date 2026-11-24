/**
 * The Olano mark — the network "O", on its own without the wordmark.
 *
 * Same geometry as `public/logo.svg` (a 1024-square coordinate space, 16 nodes
 * and the 48 edges between them), re-emitted here at a heavier stroke and a
 * larger node radius. That is not a redraw: the wordmark's weights are chosen
 * for a mark 176px tall beside 118px type, and at the 64px a sign-in card
 * gives it a 7-unit stroke resolves to half a device pixel — the logo greys
 * out into a smudge.
 *
 * Inline rather than an `<img src="/logo-mark.svg">` because this is the FIRST
 * thing painted on the sign-in screen, which is exactly the moment the box may
 * still be starting: a mark that needs a second request is a mark that
 * sometimes is not there. It also lets each surface pick the colour.
 */
export function OlanoMark({
  size = 64,
  color = "#ff6a3d",
  title,
}: {
  size?: number;
  /** Any CSS colour; `currentColor` to inherit the surrounding text. */
  color?: string;
  /**
   * Give it a title only where it is the sole identification of the product.
   * Beside a heading that already says "Olano" it is decoration, and a second
   * announcement of the same word is noise in a screen reader.
   */
  title?: string;
}) {
  return (
    <svg
      viewBox="0 0 1024 1024"
      width={size}
      height={size}
      role={title ? "img" : "presentation"}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      focusable="false"
    >
      <g
        fill={color}
        stroke={color}
        strokeWidth={20}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <line x1="355.9" y1="135.1" x2="668.1" y2="135.1" /><line x1="437.0" y1="298.3" x2="587.0" y2="298.3" /><line x1="355.9" y1="135.1" x2="437.0" y2="298.3" /><line x1="355.9" y1="135.1" x2="587.0" y2="298.3" />
        <line x1="355.9" y1="135.1" x2="330.9" y2="423.5" /><line x1="355.9" y1="135.1" x2="888.9" y2="355.9" /><line x1="668.1" y1="135.1" x2="888.9" y2="355.9" /><line x1="587.0" y1="298.3" x2="693.1" y2="423.5" />
        <line x1="668.1" y1="135.1" x2="587.0" y2="298.3" /><line x1="668.1" y1="135.1" x2="693.1" y2="423.5" /><line x1="668.1" y1="135.1" x2="437.0" y2="298.3" /><line x1="668.1" y1="135.1" x2="888.9" y2="668.1" />
        <line x1="888.9" y1="355.9" x2="888.9" y2="668.1" /><line x1="693.1" y1="423.5" x2="693.1" y2="600.5" /><line x1="888.9" y1="355.9" x2="693.1" y2="423.5" /><line x1="888.9" y1="355.9" x2="693.1" y2="600.5" />
        <line x1="888.9" y1="355.9" x2="587.0" y2="298.3" /><line x1="888.9" y1="355.9" x2="668.1" y2="888.9" /><line x1="888.9" y1="668.1" x2="668.1" y2="888.9" /><line x1="693.1" y1="600.5" x2="587.0" y2="725.7" />
        <line x1="888.9" y1="668.1" x2="693.1" y2="600.5" /><line x1="888.9" y1="668.1" x2="587.0" y2="725.7" /><line x1="888.9" y1="668.1" x2="693.1" y2="423.5" /><line x1="888.9" y1="668.1" x2="355.9" y2="888.9" />
        <line x1="668.1" y1="888.9" x2="355.9" y2="888.9" /><line x1="587.0" y1="725.7" x2="437.0" y2="725.7" /><line x1="668.1" y1="888.9" x2="587.0" y2="725.7" /><line x1="668.1" y1="888.9" x2="437.0" y2="725.7" />
        <line x1="668.1" y1="888.9" x2="693.1" y2="600.5" /><line x1="668.1" y1="888.9" x2="135.1" y2="668.1" /><line x1="355.9" y1="888.9" x2="135.1" y2="668.1" /><line x1="437.0" y1="725.7" x2="330.9" y2="600.5" />
        <line x1="355.9" y1="888.9" x2="437.0" y2="725.7" /><line x1="355.9" y1="888.9" x2="330.9" y2="600.5" /><line x1="355.9" y1="888.9" x2="587.0" y2="725.7" /><line x1="355.9" y1="888.9" x2="135.1" y2="355.9" />
        <line x1="135.1" y1="668.1" x2="135.1" y2="355.9" /><line x1="330.9" y1="600.5" x2="330.9" y2="423.5" /><line x1="135.1" y1="668.1" x2="330.9" y2="600.5" /><line x1="135.1" y1="668.1" x2="330.9" y2="423.5" />
        <line x1="135.1" y1="668.1" x2="437.0" y2="725.7" /><line x1="135.1" y1="668.1" x2="355.9" y2="135.1" /><line x1="135.1" y1="355.9" x2="355.9" y2="135.1" /><line x1="330.9" y1="423.5" x2="437.0" y2="298.3" />
        <line x1="135.1" y1="355.9" x2="330.9" y2="423.5" /><line x1="135.1" y1="355.9" x2="437.0" y2="298.3" /><line x1="135.1" y1="355.9" x2="330.9" y2="600.5" /><line x1="135.1" y1="355.9" x2="668.1" y2="135.1" />
        <circle cx="355.9" cy="135.1" r="26" /><circle cx="668.1" cy="135.1" r="26" /><circle cx="888.9" cy="355.9" r="26" /><circle cx="888.9" cy="668.1" r="26" />
        <circle cx="668.1" cy="888.9" r="26" /><circle cx="355.9" cy="888.9" r="26" /><circle cx="135.1" cy="668.1" r="26" /><circle cx="135.1" cy="355.9" r="26" />
        <circle cx="437.0" cy="298.3" r="26" /><circle cx="587.0" cy="298.3" r="26" /><circle cx="693.1" cy="423.5" r="26" /><circle cx="693.1" cy="600.5" r="26" />
        <circle cx="587.0" cy="725.7" r="26" /><circle cx="437.0" cy="725.7" r="26" /><circle cx="330.9" cy="600.5" r="26" /><circle cx="330.9" cy="423.5" r="26" />
      </g>
    </svg>
  );
}
