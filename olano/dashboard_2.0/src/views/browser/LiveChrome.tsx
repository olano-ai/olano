import { useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Camera,
  Check,
  Circle,
  FileUp,
  Globe,
  Lock,
  Plus,
  RotateCw,
  Square,
  TriangleAlert,
  X,
} from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import {
  MAX_UPLOAD_BYTES,
  MAX_UPLOAD_FILES,
  addressFor,
  addressSecurity,
  filterOptions,
  formatBytes,
  tabLabel,
} from "./liveInput";
import type { LiveBrowser, LiveChooser, LiveDialog, LivePicker, LiveSelect } from "./LiveScreen";
import { STR } from "./strings";

// The browser chrome around the live picture, shown while a person holds
// control: tabs, Back / Forward / Reload-or-Stop, the address bar, and the
// sheets for what the remote browser draws outside the page (a page's
// dialogs, its file requests, a dropdown's option list). The server side is
// olano/browser/live.py; every action here is one message on the live
// view's socket.

/** A file the browser saved for the person (POST .../browser/recording or /picture). */
interface SavedBrowserFile {
  name: string;
  relative: string;
  bytes: number;
  seconds: number;
  files_url: string;
}

/** "0:42" for a recording's length. */
export function recordingClock(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  const m = Math.floor(whole / 60);
  const s = whole % 60;
  return `${m}:${s < 10 ? "0" : ""}${s}`;
}

/** Record the browser to a video, and save a picture of the page. */
export function LiveCapture({ live }: { live: LiveBrowser }) {
  const t = useT(STR);
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!live.recording) return;
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [live.recording]);

  // The last file saved, with a way to it, shown beside the buttons for a while.
  const [lastSaved, setLastSaved] = useState<SavedBrowserFile | null>(null);
  useEffect(() => {
    if (!lastSaved) return;
    const timer = window.setTimeout(() => setLastSaved(null), 12000);
    return () => window.clearTimeout(timer);
  }, [lastSaved]);
  const saved = (file: SavedBrowserFile) => {
    const length = file.seconds > 0 ? `${recordingClock(file.seconds)}, ` : "";
    const size = file.bytes >= 1024 * 1024 ? `${(file.bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.round(file.bytes / 1024))} KB`;
    toast.push(t("br.live.saved", { name: file.relative, detail: `${length}${size}` }), "success");
    setLastSaved(file);
  };
  const call = async (path: string, body?: unknown) => {
    if (busy || !live.agent) return;
    setBusy(true);
    try {
      const result = await api.post<{ file?: SavedBrowserFile | null }>(`/api/agents/${encodeURIComponent(live.agent)}/browser/${path}`, body);
      if (result.file) saved(result.file);
    } catch (err) {
      toast.push(err instanceof ApiError ? err.detail : String(err), "error");
    } finally {
      setBusy(false);
    }
  };
  const rec = live.recording;
  return (
    <div className="browser-capture" data-testid="browser-capture">
      <button
        type="button"
        className="btn ghost sm icon"
        aria-label={t("br.live.picture")}
        title={t("br.live.picture")}
        data-testid="browser-picture"
        disabled={busy}
        onClick={() => void call("picture")}
      >
        <Camera size={15} />
      </button>
      <button
        type="button"
        className={`btn ghost sm${rec ? " browser-recording" : " icon"}`}
        aria-label={rec ? t("br.live.recordStop") : t("br.live.record")}
        title={rec ? t("br.live.recordStop") : t("br.live.record")}
        data-testid="browser-record"
        disabled={busy}
        onClick={() => void call("recording", { action: rec ? "stop" : "start" })}
      >
        {rec ? (
          <>
            <Square size={12} fill="currentColor" />
            <span className="browser-rec-clock">REC {recordingClock(now - rec.since)}</span>
          </>
        ) : (
          <Circle size={15} fill="currentColor" />
        )}
      </button>
      {lastSaved && (
        <a className="btn ghost sm browser-saved-link" href={lastSaved.files_url} data-testid="browser-saved-open" title={lastSaved.relative}>
          {t("br.live.savedOpen")}
        </a>
      )}
    </div>
  );
}

/** Tabs, navigation and the address bar. */
export function LiveNav({ live }: { live: LiveBrowser }) {
  const t = useT(STR);
  // The address bar shows the page the browser is on, as any browser's does,
  // and follows it as the page changes, except while someone is typing in it.
  const [address, setAddress] = useState(() => addressFor(live.url));
  const addressRef = useRef<HTMLInputElement | null>(null);
  const focusAddress = useRef(false);
  useEffect(() => {
    if (addressRef.current && document.activeElement === addressRef.current) return;
    setAddress(addressFor(live.url));
  }, [live.url]);
  // A new tab opens on an empty page with the address bar ready to type in,
  // once the tab exists (its state frame), so the bar is not refilled under
  // the person's first keystrokes.
  useEffect(() => {
    if (!focusAddress.current) return;
    const active = live.tabs.find((tab) => tab.active);
    if (active && !addressFor(active.url)) {
      focusAddress.current = false;
      setAddress("");
      addressRef.current?.focus();
    }
  }, [live.tabs]);

  const security = addressSecurity(live.url);
  const SecurityIcon = security === "secure" ? Lock : security === "insecure" ? TriangleAlert : Globe;
  const securityLabel =
    security === "secure"
      ? t("br.live.secure")
      : security === "insecure"
        ? t("br.live.insecure")
        : t("br.live.noAddress");

  return (
    <>
      <div className="browser-tabs" role="tablist" aria-label={t("br.live.tabs")} data-testid="browser-tabs">
        {live.tabs.map((tab) => {
          const label = tabLabel(tab) || t("br.live.newTab");
          return (
            <div key={tab.id} className={`browser-tab${tab.active ? " active" : ""}`} title={tab.url}>
              <button
                type="button"
                role="tab"
                aria-selected={tab.active}
                className="browser-tab-main"
                data-testid="browser-tab"
                onClick={() => !tab.active && live.send({ t: "tab", id: tab.id })}
              >
                {tab.active && live.loading ? (
                  <span className="browser-tab-spinner" aria-hidden="true" />
                ) : null}
                <span className="browser-tab-title">{label}</span>
              </button>
              <button
                type="button"
                className="browser-tab-close"
                aria-label={t("br.live.closeTab", { title: label })}
                title={t("br.live.closeTab", { title: label })}
                data-testid="browser-tab-close"
                onClick={() => live.send({ t: "closetab", id: tab.id })}
              >
                <X size={12} />
              </button>
            </div>
          );
        })}
        <button
          type="button"
          className="btn ghost sm icon browser-tab-new"
          aria-label={t("br.live.newTab")}
          title={t("br.live.newTab")}
          data-testid="browser-tab-new"
          onClick={() => {
            focusAddress.current = true;
            live.send({ t: "newtab" });
          }}
        >
          <Plus size={14} />
        </button>
      </div>
      <form
        className="browser-navbar"
        onSubmit={(e) => {
          e.preventDefault();
          if (address.trim()) live.send({ t: "navigate", url: address.trim() });
          // Lets the bar follow the page again (and closes a phone keyboard).
          addressRef.current?.blur();
        }}
      >
        <button
          type="button"
          className="btn ghost sm icon"
          aria-label={t("br.live.back")}
          title={t("br.live.back")}
          data-testid="browser-nav-back"
          disabled={!live.canBack}
          onClick={() => live.send({ t: "back" })}
        >
          <ArrowLeft size={15} />
        </button>
        <button
          type="button"
          className="btn ghost sm icon"
          aria-label={t("br.live.forward")}
          title={t("br.live.forward")}
          data-testid="browser-nav-forward"
          disabled={!live.canForward}
          onClick={() => live.send({ t: "forward" })}
        >
          <ArrowRight size={15} />
        </button>
        {live.loading ? (
          <button
            type="button"
            className="btn ghost sm icon"
            aria-label={t("br.live.stop")}
            title={t("br.live.stop")}
            data-testid="browser-nav-stop"
            onClick={() => live.send({ t: "stop" })}
          >
            <X size={15} />
          </button>
        ) : (
          <button
            type="button"
            className="btn ghost sm icon"
            aria-label={t("br.live.reload")}
            title={t("br.live.reload")}
            data-testid="browser-nav-reload"
            onClick={() => live.send({ t: "reload" })}
          >
            <RotateCw size={15} />
          </button>
        )}
        <label className={`browser-address ${security}`}>
          <span className="browser-address-icon" title={securityLabel}>
            <SecurityIcon size={13} aria-label={securityLabel} />
          </span>
          <input
            ref={addressRef}
            data-testid="browser-address"
            placeholder={t("br.live.urlPlaceholder")}
            aria-label={t("br.live.urlPlaceholder")}
            value={address}
            // Not type="url": the form would then refuse "example.com", which
            // the server completes to https://example.com.
            inputMode="url"
            enterKeyHint="go"
            autoComplete="off"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            // Selected on focus, so typing replaces it and a tap can still
            // place the caret to edit it.
            onFocus={(e) => e.currentTarget.select()}
            onChange={(e) => setAddress(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                setAddress(addressFor(live.url));
                e.currentTarget.blur();
              }
            }}
          />
        </label>
        <button type="submit" className="btn sm" data-testid="browser-nav-go">
          {t("br.live.go")}
        </button>
        <LiveCapture live={live} />
      </form>
    </>
  );
}

/** Whichever sheet the page is waiting on: its dialog first (the page takes
 *  nothing else until it is answered), then a file request, then a list. */
export function LivePrompts({ live }: { live: LiveBrowser }) {
  if (live.controller !== "user") return null;
  if (live.dialog) return <DialogSheet key={live.dialog.id} live={live} dialog={live.dialog} />;
  if (live.chooser) return <FileSheet key={live.chooser.id} live={live} chooser={live.chooser} />;
  if (live.select) return <SelectSheet live={live} select={live.select} />;
  if (live.picker) return <PickerSheet live={live} picker={live.picker} />;
  return null;
}

function Sheet({
  title,
  onCancel,
  children,
  testId,
}: {
  title: string;
  onCancel: () => void;
  children: React.ReactNode;
  testId: string;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);
  return (
    <div
      className="browser-sheet-backdrop"
      onPointerDown={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div className="browser-sheet" role="dialog" aria-modal="true" aria-label={title} data-testid={testId}>
        <div className="browser-sheet-title">{title}</div>
        {children}
      </div>
    </div>
  );
}

function DialogSheet({ live, dialog }: { live: LiveBrowser; dialog: LiveDialog }) {
  const t = useT(STR);
  const [text, setText] = useState(dialog.default);
  const answer = (accept: boolean) =>
    live.send({ t: "dialog", id: dialog.id, accept, ...(dialog.kind === "prompt" ? { text } : {}) });
  const leaving = dialog.kind === "beforeunload";
  const title =
    dialog.kind === "confirm"
      ? t("br.live.dialog.confirm")
      : dialog.kind === "prompt"
        ? t("br.live.dialog.prompt")
        : leaving
          ? t("br.live.dialog.leave")
          : t("br.live.dialog.alert");
  return (
    <Sheet title={title} onCancel={() => answer(dialog.kind === "alert")} testId="browser-dialog">
      {dialog.message && <div className="browser-sheet-message">{dialog.message}</div>}
      {leaving && !dialog.message && (
        <div className="browser-sheet-message">{t("br.live.dialog.leaveBody")}</div>
      )}
      {dialog.kind === "prompt" && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            answer(true);
          }}
        >
          <input
            className="input"
            autoFocus
            value={text}
            onChange={(e) => setText(e.target.value)}
            data-testid="browser-dialog-text"
          />
        </form>
      )}
      <div className="browser-sheet-actions">
        {dialog.kind !== "alert" && (
          <button type="button" className="btn ghost sm" onClick={() => answer(false)} data-testid="browser-dialog-cancel">
            {leaving ? t("br.live.dialog.stay") : t("br.live.dialog.cancel")}
          </button>
        )}
        <button
          type="button"
          className="btn primary sm"
          autoFocus={dialog.kind !== "prompt"}
          onClick={() => answer(true)}
          data-testid="browser-dialog-ok"
        >
          {leaving ? t("br.live.dialog.leaveOk") : t("br.live.dialog.ok")}
        </button>
      </div>
    </Sheet>
  );
}

function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result || "");
      resolve(url.slice(url.indexOf(",") + 1));
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function FileSheet({ live, chooser }: { live: LiveBrowser; chooser: LiveChooser }) {
  const t = useT(STR);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  // The page refused what was sent (the request is still open): show why and
  // let the person choose again, rather than leaving "Sending..." up.
  const notice = live.notice;
  useEffect(() => {
    if (notice && busy) {
      setBusy(false);
      setProblem(notice);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only a new notice counts
  }, [notice]);
  const cancel = () => live.send({ t: "files", id: chooser.id, files: [] });
  const onPick = async (e: ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files || []);
    e.target.value = "";
    if (!picked.length) return;
    if (picked.length > MAX_UPLOAD_FILES) {
      setProblem(t("br.live.files.tooMany", { max: String(MAX_UPLOAD_FILES) }));
      return;
    }
    const total = picked.reduce((n, f) => n + f.size, 0);
    if (total > MAX_UPLOAD_BYTES) {
      setProblem(t("br.live.files.tooBig", { size: formatBytes(total), max: formatBytes(MAX_UPLOAD_BYTES) }));
      return;
    }
    setBusy(true);
    setProblem("");
    try {
      const files = await Promise.all(
        picked.map(async (f) => ({ name: f.name, mime: f.type, data: await readAsBase64(f) })),
      );
      live.send({ t: "files", id: chooser.id, files });
    } catch {
      setBusy(false);
      setProblem(t("br.live.files.unreadable"));
    }
  };
  return (
    <Sheet
      title={chooser.multiple ? t("br.live.files.titleMany") : t("br.live.files.title")}
      onCancel={cancel}
      testId="browser-files"
    >
      <div className="browser-sheet-message">
        {t("br.live.files.body", { max: formatBytes(MAX_UPLOAD_BYTES) })}
        {chooser.accept && (
          <div className="muted" style={{ marginTop: 4 }}>
            {t("br.live.files.accept", { accept: chooser.accept })}
          </div>
        )}
      </div>
      {problem && <div className="browser-live-notice">{problem}</div>}
      <input
        ref={inputRef}
        type="file"
        hidden
        multiple={chooser.multiple}
        accept={chooser.accept || undefined}
        onChange={(e) => void onPick(e)}
        data-testid="browser-files-input"
      />
      <div className="browser-sheet-actions">
        <button type="button" className="btn ghost sm" onClick={cancel} data-testid="browser-files-cancel">
          {t("br.live.dialog.cancel")}
        </button>
        <button
          type="button"
          className="btn primary sm"
          disabled={busy}
          onClick={() => inputRef.current?.click()}
          data-testid="browser-files-choose"
        >
          <FileUp size={14} /> {busy ? t("br.live.files.sending") : t("br.live.files.choose")}
        </button>
      </div>
    </Sheet>
  );
}

// Lists longer than this get a filter box (a country list is ~250 long).
const FILTER_FROM = 12;

function SelectSheet({ live, select }: { live: LiveBrowser; select: LiveSelect }) {
  const t = useT(STR);
  const [query, setQuery] = useState("");
  const shown = useMemo(() => filterOptions(select.options, query), [select.options, query]);
  const listRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    // Open the list where the current choice is, as the page's own list would.
    listRef.current?.querySelector<HTMLElement>("[aria-selected=true]")?.scrollIntoView({ block: "center" });
  }, []);
  let lastGroup = "";
  return (
    <Sheet title={select.label || t("br.live.select.title")} onCancel={live.clearSelect} testId="browser-select">
      {select.options.length > FILTER_FROM && (
        <input
          className="input"
          placeholder={t("br.live.select.filter")}
          aria-label={t("br.live.select.filter")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          data-testid="browser-select-filter"
        />
      )}
      <div className="browser-select-list" role="listbox" ref={listRef}>
        {shown.map(({ option, index }) => {
          const header = option.group && option.group !== lastGroup ? option.group : "";
          lastGroup = option.group;
          return (
            <div key={index}>
              {header && <div className="browser-select-group">{header}</div>}
              <button
                type="button"
                role="option"
                aria-selected={index === select.selected}
                disabled={option.disabled}
                className={`browser-select-option${index === select.selected ? " selected" : ""}`}
                data-testid="browser-select-option"
                onClick={() => {
                  live.send({ t: "select", index });
                  live.clearSelect();
                }}
              >
                <span className="grow">{option.label || " "}</span>
                {index === select.selected && <Check size={14} aria-hidden="true" />}
              </button>
            </div>
          );
        })}
        {!shown.length && <div className="muted browser-select-empty">{t("br.live.select.none")}</div>}
      </div>
      <div className="browser-sheet-actions">
        <button type="button" className="btn ghost sm" onClick={live.clearSelect}>
          {t("br.live.dialog.cancel")}
        </button>
      </div>
    </Sheet>
  );
}

/** The input type this browser draws for a page's picker: the same kind where it has one, a text box for the rest. */
function pickerInputType(kind: string): string {
  return ["date", "time", "datetime-local", "month", "week", "color"].includes(kind) ? kind : "text";
}

/** A date, time or colour field the click landed on: this browser's own picker, sent back as the value. */
function PickerSheet({ live, picker }: { live: LiveBrowser; picker: LivePicker }) {
  const t = useT(STR);
  const [value, setValue] = useState(picker.value);
  const set = () => {
    live.send({ t: "setvalue", value: value.trim() });
    live.clearPicker();
  };
  return (
    <Sheet title={picker.label || t("br.live.picker.title")} onCancel={live.clearPicker} testId="browser-picker">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          set();
        }}
      >
        <input
          className="input"
          type={pickerInputType(picker.kind)}
          value={value}
          min={picker.min || undefined}
          max={picker.max || undefined}
          step={picker.step || undefined}
          aria-label={picker.label || t("br.live.picker.title")}
          onChange={(e) => setValue(e.target.value)}
          data-testid="browser-picker-input"
          autoFocus
        />
        <div className="browser-sheet-actions">
          <button type="button" className="btn ghost sm" onClick={live.clearPicker}>
            {t("br.live.dialog.cancel")}
          </button>
          <button type="submit" className="btn sm" data-testid="browser-picker-set">
            {t("br.live.picker.set")}
          </button>
        </div>
      </form>
    </Sheet>
  );
}
