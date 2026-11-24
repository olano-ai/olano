// Pure helpers for the Memory view: format byte sizes and parse the raw
// MEMORY.md text into structured sections/bullets so the dashboard can render a
// browsable outline alongside the raw markdown. These are deliberately
// dependency-free and mirror the on-disk format produced by
// olano/memory/markdown_memory.py (## H2 sections + "- [YYYY-MM-DD] …"
// bullets). No backend round-trip is required — everything is derived from the
// single GET /api/memory payload.

export interface MemoryBullet {
  /** The raw line, marker stripped (e.g. "[2026-01-01] Uses Python"). */
  text: string;
  /** Extracted YYYY-MM-DD date stamp, or "" when the bullet has none. */
  date: string;
  /** Bullet body with the leading [date] removed, for compact display. */
  body: string;
}

export interface MemorySection {
  /** H2 heading text (without the "## "). */
  name: string;
  /** Bullet lines under this heading. */
  bullets: MemoryBullet[];
  /** Non-bullet prose lines under this heading (preserved verbatim). */
  prose: string[];
}

export interface ParsedMemory {
  /** Leading prose before the first H2 (title, intro blurb, …). */
  intro: string[];
  sections: MemorySection[];
  /** Total bullet count across every section. */
  totalBullets: number;
}

const BULLET_RE = /^[-*]\s+(.*)$/;
const DATE_RE = /\[(\d{4}-\d{2}-\d{2})\]/;

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** Parse MEMORY.md text into intro + H2 sections with bullets and prose. */
export function parseMemory(raw: string): ParsedMemory {
  const lines = raw.split("\n");
  const intro: string[] = [];
  const sections: MemorySection[] = [];
  let current: MemorySection | null = null;
  let totalBullets = 0;

  for (const line of lines) {
    const heading = /^##\s+(.*)$/.exec(line.trim());
    if (heading) {
      current = { name: heading[1].trim(), bullets: [], prose: [] };
      sections.push(current);
      continue;
    }
    const bulletMatch = BULLET_RE.exec(line.trim());
    if (bulletMatch) {
      const text = bulletMatch[1].trim();
      const dateMatch = DATE_RE.exec(text);
      const date = dateMatch ? dateMatch[1] : "";
      const body = date ? text.replace(DATE_RE, "").trim() : text;
      const bullet: MemoryBullet = { text, date, body };
      if (current) current.bullets.push(bullet);
      else intro.push(line);
      totalBullets += 1;
      continue;
    }
    if (current) {
      if (line.trim()) current.prose.push(line);
    } else if (line.trim()) {
      intro.push(line);
    }
  }

  return { intro, sections, totalBullets };
}

/** Case-insensitive substring match against a bullet's full text. */
export function bulletMatches(bullet: MemoryBullet, query: string): boolean {
  if (!query) return true;
  return bullet.text.toLowerCase().includes(query.toLowerCase());
}

/** Section matches if its name matches, or any of its bullets match. */
export function sectionMatches(section: MemorySection, query: string): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  if (section.name.toLowerCase().includes(q)) return true;
  return section.bullets.some((b) => b.text.toLowerCase().includes(q));
}
