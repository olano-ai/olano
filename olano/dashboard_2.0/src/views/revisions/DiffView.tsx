// Renders real `git diff` output with react-diff-view: split (side by side)
// or unified, with in-line word edits marked. The backend always sends plain
// git patches (engine.file_diff, remote.merge_file), so this component never
// computes a diff itself.

import { useMemo } from "react";
import { Diff, Hunk, markEdits, parseDiff, tokenize, type FileData, type ViewType } from "react-diff-view";
import "react-diff-view/style/index.css";

function FileBlock({ file, viewType }: { file: FileData; viewType: ViewType }) {
  const tokens = useMemo(() => {
    try {
      return tokenize(file.hunks, { enhancers: [markEdits(file.hunks, { type: "block" })] });
    } catch {
      return undefined; // word marks are a nicety; the diff renders without them
    }
  }, [file.hunks]);
  return (
    <Diff viewType={viewType} diffType={file.type} hunks={file.hunks} tokens={tokens} className="revisions-diff-table">
      {(hunks) => hunks.map((hunk) => <Hunk key={hunk.content} hunk={hunk} />)}
    </Diff>
  );
}

export function DiffView({
  patch,
  viewType,
  emptyText,
  labels,
}: {
  patch: string;
  viewType: ViewType;
  emptyText: string;
  /** Column captions over a split view, e.g. ["This box", "GitHub"]. */
  labels?: [string, string];
}) {
  const files = useMemo(() => {
    try {
      return parseDiff(patch, { nearbySequences: "zip" });
    } catch {
      return [];
    }
  }, [patch]);
  const withHunks = files.filter((f) => f.hunks.length > 0);
  if (withHunks.length === 0) {
    return <p className="muted revisions-diff-empty">{emptyText}</p>;
  }
  return (
    <div className={`revisions-diff revisions-diff--${viewType}`}>
      {labels && viewType === "split" ? (
        <div className="revisions-diff-labels" aria-hidden="true">
          <span>{labels[0]}</span>
          <span>{labels[1]}</span>
        </div>
      ) : null}
      {withHunks.map((file, i) => (
        <FileBlock key={`${file.oldPath}-${file.newPath}-${i}`} file={file} viewType={viewType} />
      ))}
    </div>
  );
}
