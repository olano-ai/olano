import { Download, File, FileCode, FileSpreadsheet, FileText, FolderOpen, X } from "lucide-react";
import { Link } from "react-router-dom";
import type { MediaRef } from "../../core/types";
import type { Attachment } from "./attachments";
import { fmtSize } from "./attachments";
import { useT } from "../../core/i18n";
import { STR } from "./strings";

// Renders a single attachment as inline media (image / audio / video) or a file
// chip. Used both in sent message bubbles and (with a remove button) in the
// composer's staged tray.
export function AttachmentMedia({ att }: { att: Attachment }) {
  if (att.kind === "image" && att.preview) {
    return <img className="att-image" src={att.preview} alt={att.name} loading="lazy" />;
  }
  if (att.kind === "audio" && att.preview) {
    return <audio className="att-audio" controls src={att.preview} />;
  }
  if (att.kind === "video" && att.preview) {
    return <video className="att-video" controls src={att.preview} />;
  }
  return (
    <span className="att-file" title={att.name}>
      <FileText size={14} strokeWidth={1.75} />
      <span className="att-file-name">{att.name}</span>
      <span className="att-file-size">{fmtSize(att.size)}</span>
    </span>
  );
}

type T = ReturnType<typeof useT>;

// The glyph a document/file card wears. Extension-led and cheap: a spreadsheet
// or a source file reads as one at a glance; everything else follows the
// backend's kind (document vs. generic file).
const SPREADSHEET_EXT = /^(csv|tsv|xls|xlsx|xlsm|ods|numbers)$/;
const CODE_EXT =
  /^(py|js|jsx|ts|tsx|mjs|cjs|json|yaml|yml|toml|sh|bash|zsh|rb|go|rs|java|kt|c|h|cpp|hpp|cs|php|sql|html|css|scss|xml|ini|cfg|env)$/;
function fileExt(item: MediaRef): string {
  const dot = item.name.lastIndexOf(".");
  return dot > 0 ? item.name.slice(dot + 1).toLowerCase() : "";
}
function fileIcon(item: MediaRef) {
  const ext = fileExt(item);
  if (SPREADSHEET_EXT.test(ext)) return FileSpreadsheet;
  if (CODE_EXT.test(ext)) return FileCode;
  return item.kind === "document" ? FileText : File;
}

// Download + Open in Files — under every media item and inside every file
// card. Download goes through `download_url` (served as an attachment) in a
// new tab; Open in Files is a client-side route change to the Files tab with
// the file previewed (`files_url`). Either is omitted when the backend sent
// no such URL (an older runtime, a file outside the workspace).
function MediaActions({ item, t }: { item: MediaRef; t: T }) {
  if (!item.download_url && !item.files_url) return null;
  return (
    <span className="chat-media-actions">
      {item.download_url && (
        <a
          className="chat-media-action"
          href={item.download_url}
          download={item.name}
          target="_blank"
          rel="noreferrer"
          title={t("att.download")}
        >
          <Download size={12} strokeWidth={2} /> {t("att.download")}
        </a>
      )}
      {item.files_url && (
        <Link className="chat-media-action" to={item.files_url} title={t("att.openInFiles")}>
          <FolderOpen size={12} strokeWidth={2} /> {t("att.openInFiles")}
        </Link>
      )}
    </span>
  );
}

// Name, size and the actions — the caption row under an image/audio/video.
function MediaCaption({ item, t }: { item: MediaRef; t: T }) {
  return (
    <div className="chat-media-caption">
      <span className="chat-media-name" title={item.path}>
        {item.name}
      </span>
      {item.size !== undefined && <span className="chat-media-size">{fmtSize(item.size)}</span>}
      <MediaActions item={item} t={t} />
    </div>
  );
}

// A single workspace file the agent produced or named this turn, streamed from
// `/api/files-raw` (same-origin, session cookie). An image is a thumbnail that
// opens the file in the Files tab (the raw file in a new tab when there is no
// deep link); audio/video get native controls; a document or any other file
// is a card chip. Every one carries the caption row above.
function AgentMediaItem({ item, t }: { item: MediaRef; t: T }) {
  if (item.kind === "audio") {
    return (
      <div className="chat-media-item">
        <audio className="chat-media-audio" controls preload="none" src={item.url} />
        <MediaCaption item={item} t={t} />
      </div>
    );
  }
  if (item.kind === "video") {
    return (
      <div className="chat-media-item">
        <video className="chat-media-video" controls preload="metadata" src={item.url} />
        <MediaCaption item={item} t={t} />
      </div>
    );
  }
  if (item.kind === "image") {
    const img = <img className="chat-media-img" src={item.url} alt={item.name} loading="lazy" />;
    return (
      <div className="chat-media-item">
        {item.files_url ? (
          <Link className="chat-media-link" to={item.files_url} title={item.name}>
            {img}
          </Link>
        ) : (
          <a className="chat-media-link" href={item.url} target="_blank" rel="noreferrer" title={item.name}>
            {img}
          </a>
        )}
        <MediaCaption item={item} t={t} />
      </div>
    );
  }
  const Icon = fileIcon(item);
  const ext = fileExt(item);
  const meta = [ext ? ext.toUpperCase() : "", item.size !== undefined ? fmtSize(item.size) : ""]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="chat-file-card" title={item.path}>
      <Icon className="chat-file-icon" size={20} strokeWidth={1.6} />
      <span className="chat-file-main">
        <span className="chat-file-name">
          {item.files_url ? (
            <Link to={item.files_url}>{item.name}</Link>
          ) : (
            <a href={item.url} target="_blank" rel="noreferrer">
              {item.name}
            </a>
          )}
        </span>
        {meta && <span className="chat-file-meta">{meta}</span>}
      </span>
      <MediaActions item={item} t={t} />
    </div>
  );
}

// Files produced or named by the agent (screenshots, generated images, TTS
// clips, the report it just wrote), rendered beneath the message. Kept
// separate from user MessageAttachments because these are server-hosted URLs,
// not base64 previews.
export function AgentMedia({ items }: { items: MediaRef[] }) {
  const t = useT(STR);
  if (!items.length) return null;
  return (
    <div className="att-row chat-media-row">
      {items.map((m) => (
        <AgentMediaItem key={m.url} item={m} t={t} />
      ))}
    </div>
  );
}

// Attachments attached to a sent message (read-only).
export function MessageAttachments({ items }: { items: Attachment[] }) {
  if (!items.length) return null;
  return (
    <div className="att-row">
      {items.map((a) => (
        <AttachmentMedia key={a.id} att={a} />
      ))}
    </div>
  );
}

// Staged attachments in the composer, each with a remove button.
export function StagedAttachments({
  items,
  onRemove,
}: {
  items: Attachment[];
  onRemove: (id: string) => void;
}) {
  const t = useT(STR);
  if (!items.length) return null;
  return (
    <div className="att-row staged">
      {items.map((a) => (
        <span className="att-staged" key={a.id}>
          <AttachmentMedia att={a} />
          <button className="att-remove" aria-label={t("att.remove")} onClick={() => onRemove(a.id)}>
            <X size={12} strokeWidth={2.5} />
          </button>
        </span>
      ))}
    </div>
  );
}
