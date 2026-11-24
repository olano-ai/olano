/**
 * One plain-language line per tool step ("Searched the web", "Read notes.md"),
 * the way a phone shows progress: a verb and the one argument a person would
 * recognise, never a function name and its JSON.
 *
 * Names are matched on their last segment with MCP and namespace prefixes
 * stripped (`mcp__github__create_issue` reads as "create issue"), so a tool
 * this table has never heard of still gets a readable line.
 *
 * Pure, so `node --test` covers it.
 */

export type StepIcon = "search" | "web" | "file" | "edit" | "terminal" | "agent" | "memory" | "mail" | "calendar" | "image" | "link" | "tool";

export interface StepLabel {
  icon: StepIcon;
  /** Past tense once the step has a result, progressive while it runs. */
  label: string;
  /** The salient argument: a query, a file name, a command. */
  detail?: string;
}

interface Rule {
  test: RegExp;
  icon: StepIcon;
  running: string;
  done: string;
  keys: string[];
}

const RULES: Rule[] = [
  { test: /^(web_?search|search_web|tavily|internet_search|parallel_search)/, icon: "search", running: "Searching the web", done: "Searched the web", keys: ["query", "q", "objective"] },
  { test: /^(fetch|web_?fetch|browse|open_url|http_get|read_url|scrape)/, icon: "web", running: "Opening a page", done: "Opened a page", keys: ["url", "urls"] },
  { test: /^browser_navigate/, icon: "web", running: "Opening a page in the browser", done: "Opened a page in the browser", keys: ["url"] },
  { test: /^(browser_click|browser_press_key)$/, icon: "web", running: "Clicking in the browser", done: "Clicked in the browser", keys: ["target", "selector", "text", "key"] },
  { test: /^(browser_type|browser_fill)$/, icon: "web", running: "Typing in the browser", done: "Typed in the browser", keys: ["target", "selector"] },
  { test: /^(browser_screenshot|page_screenshot)$/, icon: "image", running: "Taking a screenshot", done: "Took a screenshot", keys: ["url"] },
  { test: /^browser_request_user$/, icon: "web", running: "Asking you to take over the browser", done: "Asked you to take over the browser", keys: ["reason"] },
  { test: /^(browser_snapshot|browser_extract)$/, icon: "web", running: "Reading the page", done: "Read the page", keys: ["url"] },
  { test: /^(browser_)/, icon: "web", running: "Using the browser", done: "Used the browser", keys: ["url", "text", "selector"] },
  { test: /^magic_link_(connect_service|connect_channels)$/, icon: "link", running: "Making a sign-in link", done: "Made a sign-in link", keys: ["service", "label"] },
  { test: /^magic_link_request_secret$/, icon: "link", running: "Making a link to enter a key", done: "Made a link to enter a key", keys: ["name", "label"] },
  { test: /^magic_link_(request_file_edit|edit_instructions)$/, icon: "link", running: "Making a link to edit", done: "Made a link to edit", keys: ["path", "label"] },
  { test: /^magic_link_status$/, icon: "link", running: "Checking a link", done: "Checked a link", keys: ["link_id"] },
  { test: /^magic_link_/, icon: "link", running: "Making a link", done: "Made a link", keys: ["label"] },
  { test: /^(self|olano)_authorize_mcp_server$/, icon: "link", running: "Making a sign-in link", done: "Made a sign-in link", keys: ["name", "server"] },
  { test: /^(read_file|read|cat|view_file|open_file)$/, icon: "file", running: "Reading", done: "Read", keys: ["file_path", "path", "file"] },
  { test: /^(write_file|write|create_file)$/, icon: "edit", running: "Writing", done: "Wrote", keys: ["file_path", "path", "file"] },
  { test: /^(edit_file|edit|str_replace|apply_patch)$/, icon: "edit", running: "Editing", done: "Edited", keys: ["file_path", "path", "file"] },
  { test: /^(ls|glob|grep|find|list_files|search_files)$/, icon: "file", running: "Looking through files", done: "Looked through files", keys: ["pattern", "path", "query"] },
  { test: /^(execute|shell|safe_shell|bash|run_command|terminal|node_run|python)/, icon: "terminal", running: "Running a command", done: "Ran a command", keys: ["command", "cmd", "code"] },
  { test: /^(talk_to_agent|talk_to|ask_|a2a_send_message)/, icon: "agent", running: "Asking", done: "Asked", keys: ["agent_name", "agent", "name"] },
  { test: /^task$/, icon: "agent", running: "Delegating", done: "Delegated", keys: ["subagent_type", "description"] },
  { test: /^(memory_|remember|recall|rag_)/, icon: "memory", running: "Checking memory", done: "Checked memory", keys: ["query", "fact", "text"] },
  { test: /^(gmail_|email_|send_email|outlook_)/, icon: "mail", running: "Working on email", done: "Worked on email", keys: ["subject", "to", "query"] },
  { test: /^(calendar_|gcal_|google_calendar)/, icon: "calendar", running: "Checking the calendar", done: "Checked the calendar", keys: ["summary", "title", "query"] },
  { test: /^(image_generate|image_edit|render_html_image|render_html_pdf|render_html_svg|generate_image)/, icon: "image", running: "Making an image", done: "Made an image", keys: ["prompt", "filename", "path"] },
  { test: /^(image_describe|vision)/, icon: "image", running: "Looking at an image", done: "Looked at an image", keys: ["path", "question"] },
];

/** `mcp__github__create_issue` → `create_issue`; `github.create_issue` → `create_issue`. */
export function baseName(name: string): string {
  const parts = name.split(/__|[./:]/).filter(Boolean);
  return (parts[parts.length - 1] || name).toLowerCase();
}

function parseArgs(args: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(args);
    return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function shorten(text: string, max = 60): string {
  const one = text.replace(/\s+/g, " ").trim();
  return one.length > max ? `${one.slice(0, max - 1)}…` : one;
}

function fileName(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts[parts.length - 1] || path;
}

function pick(args: Record<string, unknown> | null, keys: string[]): string | undefined {
  if (!args) return undefined;
  for (const key of keys) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return value;
    if (Array.isArray(value) && typeof value[0] === "string") return value[0];
  }
  return undefined;
}

function humanize(name: string): string {
  const words = baseName(name).replace(/[_-]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "A step";
}

export function describeStep(name: string, args: string, done: boolean): StepLabel {
  const base = baseName(name);
  const parsed = parseArgs(args);
  const rule = RULES.find((r) => r.test.test(base));
  if (!rule) {
    const detail = pick(parsed, ["query", "path", "url", "title", "name", "subject"]);
    return { icon: "tool", label: humanize(name), detail: detail ? shorten(detail) : undefined };
  }
  let detail = pick(parsed, rule.keys);
  if (detail && (rule.icon === "file" || rule.icon === "edit")) detail = fileName(detail);
  if (detail && rule.icon === "web") {
    try {
      detail = new URL(detail).host;
    } catch {
      // Not a URL: keep the text.
    }
  }
  return { icon: rule.icon, label: done ? rule.done : rule.running, detail: detail ? shorten(detail) : undefined };
}
