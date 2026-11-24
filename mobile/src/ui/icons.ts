/**
 * Icons the app needs that ionicons has no match for, as data URLs an
 * `<IonIcon>` takes. Drawn like the dashboard's (lucide, ISC licence), so the
 * same thing looks the same in both.
 */

function svg(paths: string[], strokeWidth = 2): string {
  const body = paths.map((d) => `<path d="${d}" fill="none" stroke="currentColor" stroke-width="${strokeWidth}" stroke-linecap="round" stroke-linejoin="round"/>`).join("");
  // Unencoded, the way ionicons ships its own: it reads the markup straight
  // out of the URL after ";utf8,".
  return `data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">${body}</svg>`;
}

const BRAIN = [
  "M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z",
  "M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z",
  "M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4",
  "M17.599 6.5a3 3 0 0 0 .399-1.375",
  "M6.003 5.125A3 3 0 0 0 6.401 6.5",
  "M3.477 10.896a4 4 0 0 1 .585-.396",
  "M19.938 10.5a4 4 0 0 1 .585.396",
  "M6 18a4 4 0 0 1-1.967-.516",
  "M19.967 17.484A4 4 0 0 1 18 18",
];

/** Cortex: the dashboard's brain. */
export const brain = svg(BRAIN, 1.75);
/** The same, a touch heavier, for the selected tab. */
export const brainBold = svg(BRAIN, 2.25);
