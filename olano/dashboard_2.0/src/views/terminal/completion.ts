// Command suggestions for the dashboard terminal.
//
// Scope, deliberately: this is NOT a completion engine. Path completion belongs
// to the shell — the PTY runs an interactive bash, so Tab already expands
// filenames through readline exactly as it does in any terminal, and nothing
// here tries to duplicate that (a client-side guess would have to invent a
// filesystem it cannot see, and would disagree with the shell the moment a
// command changed directory). What the browser CAN add is recall: the commands
// this operator has actually run, offered as a prefix match while they type.
// That is the whole feature, and it is why the accept key is `→` (fish-style,
// at end of line) rather than Tab — Tab must keep reaching the shell.
//
// The line model below exists because a PTY is a byte stream, not a form: to
// know what the user is typing we replay their keystrokes into a local buffer.
// Anything we cannot replay faithfully (Tab, history recall, a cursor move, any
// escape sequence) marks the model INVALID and suppresses suggestions until the
// next fresh prompt — a wrong suggestion that inserts the wrong delta is much
// worse than no suggestion, and "valid" is the only thing keeping the accepted
// text and the shell's own line buffer in agreement.

const HISTORY_KEY = "lc.term.history";
const HISTORY_MAX = 200;
const SUGGEST_MAX = 5;
// Two chars before offering anything: on one letter almost every entry matches,
// so the strip becomes a menu of unrelated commands rather than a recall aid.
const MIN_PREFIX = 2;

// Offered before this browser has any history of its own, so the strip is
// useful on a fresh box instead of empty. Kept generic — anything box-specific
// belongs in the user's own history, which outranks these.
const SEED_COMMANDS = [
  "ls -la",
  "cd ..",
  "pwd",
  "df -h",
  "du -sh *",
  "ps aux",
  "git status",
  "git log --oneline -20",
  "git diff",
  "tail -f",
  "grep -rn ",
  "tmux new -s work",
  "tmux attach -t work",
  "tmux ls",
];

function readHistory(): string[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((x): x is string => typeof x === "string").slice(0, HISTORY_MAX);
  } catch {
    // storage disabled / corrupt entry — start empty rather than throw into a
    // keystroke handler.
    return [];
  }
}

// Most-recent-first, deduped. Kept in memory so a keystroke never parses JSON.
let history: string[] = readHistory();

function persist(): void {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
  } catch {
    // storage disabled / quota — the in-memory list still serves this session.
  }
}

/** Record a command the user actually submitted (most recent wins). */
export function rememberCommand(command: string): void {
  const cmd = command.trim();
  // A leading space is the shell's own "don't record this" convention
  // (HISTCONTROL=ignorespace); honour it — that is how secrets get typed.
  if (!cmd || command.startsWith(" ") || cmd.length > 500) return;
  history = [cmd, ...history.filter((c) => c !== cmd)].slice(0, HISTORY_MAX);
  persist();
}

/** History + seeds matching `line` as a prefix, most recent first. */
export function suggestFor(line: string): string[] {
  if (line.length < MIN_PREFIX || line.startsWith(" ")) return [];
  const out: string[] = [];
  for (const cmd of [...history, ...SEED_COMMANDS]) {
    // Case-sensitive on purpose: accepting a suggestion sends only the delta
    // (`cmd.slice(line.length)`), which is only correct when the visible prefix
    // is character-identical to what the shell already holds.
    if (cmd.length > line.length && cmd.startsWith(line) && !out.includes(cmd)) {
      out.push(cmd);
      if (out.length >= SUGGEST_MAX) break;
    }
  }
  return out;
}

/**
 * A replay of the user's keystrokes into the shell's current input line.
 *
 * `valid` is false whenever the replay can no longer be trusted; callers must
 * not suggest (or accept) while it is.
 */
export class LineTracker {
  line = "";
  valid = true;
  /** Escape dismissed the strip for this line; cleared as soon as it changes. */
  dismissed = false;

  /** Feed one chunk of user→PTY data (an xterm `onData` payload). */
  feed(data: string): void {
    for (let i = 0; i < data.length; i++) {
      const ch = data[i];
      if (ch === "\x1b") {
        // Arrow keys, history recall, Ctrl-R, a mouse report… the shell's line
        // is about to become something we didn't type. Give up on the rest of
        // the chunk too: the bytes after ESC are that sequence's payload.
        this.valid = false;
        return;
      }
      if (ch === "\r" || ch === "\n") {
        this.commit();
        continue;
      }
      if (ch === "\x7f" || ch === "\b") {
        this.line = this.line.slice(0, -1);
        this.dismissed = false;
        continue;
      }
      if (ch === "\x03") {
        // Ctrl-C — the shell abandons the line and prints a fresh prompt, which
        // is a state we CAN model exactly.
        this.reset();
        continue;
      }
      if (ch === "\x15") {
        this.line = "";
        this.dismissed = false;
        continue;
      }
      if (ch === "\x17") {
        this.line = this.line.replace(/\s*\S*$/, "");
        this.dismissed = false;
        continue;
      }
      if (ch === "\t") {
        // readline is about to rewrite the line with a completion we can't see.
        this.valid = false;
        continue;
      }
      if (ch === "\x0c") {
        // Ctrl-L clears the screen and redraws the SAME line — the one control
        // character that provably leaves our model correct.
        continue;
      }
      if (ch < " ") {
        this.valid = false;
        continue;
      }
      this.line += ch;
      this.dismissed = false;
    }
  }

  /** Append literal text (a paste), which never carries key semantics. */
  feedText(text: string): void {
    if (/[\r\n]/.test(text)) {
      // A multi-line paste executes lines as it goes; we no longer know where
      // the shell ended up.
      this.valid = false;
      return;
    }
    this.feed(text);
  }

  /**
   * Enter: bank the command (when we trust the line) and start a new one.
   *
   * A line the model lost track of is deliberately NOT remembered — a command
   * finished with Tab-completion or recalled with ↑ stays out of the strip.
   * Recovering it would mean scraping the rendered row and guessing where the
   * prompt ends, and a recall list with subtly wrong entries is worse than one
   * that only holds what it watched being typed.
   */
  commit(): void {
    if (this.valid) rememberCommand(this.line);
    this.reset();
  }

  reset(): void {
    this.line = "";
    this.valid = true;
    this.dismissed = false;
  }

  /** Suggestions for the current line, or none when the model is untrusted. */
  suggestions(): string[] {
    if (!this.valid || this.dismissed) return [];
    return suggestFor(this.line);
  }
}
