import { Component, type ReactNode } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";
import { isChunkLoadError } from "../core/chunkRecovery";
import { hardReset } from "../core/pwa";

// Error boundary for the routed-view area. Every view is a React.lazy chunk
// (app.tsx): a failed chunk fetch — deploy skew purging old hashed assets, a
// network blip mid-navigation — rejects the import and, without a boundary,
// unmounts the entire tree (white screen). This catches it and offers a
// reload, which fetches the current asset manifest and recovers. Mount it
// keyed by route path so navigating elsewhere clears the error and retries.
//
// By the time a MISSING chunk reaches here, core/chunkRecovery has already
// declined to auto-reload — the tab reloaded once already, or the origin
// stopped answering. So the copy points at the two things that are actually
// left: reload by hand, or come back when the connection is up. A view that
// loaded and then threw is a different failure and says so.
//
// Class component by necessity: error boundaries have no hook equivalent.
// Copy is hardcoded English on purpose — this surface must render even if the
// i18n provider itself was part of the failed tree.

interface State {
  error: Error | null;
}

export class RouteErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  render() {
    if (!this.state.error) return this.props.children;
    const chunk = isChunkLoadError(this.state.error);
    const explain = chunk
      ? " — this view's code could not be downloaded. It usually means the dashboard was updated" +
        " (reloading fetches the new version) or the connection to the server dropped."
      : " — this view failed while rendering. Reloading may clear it; if it repeats, the error" +
        " above is the bug.";
    return (
      <div className="page">
        <div
          className="card"
          role="alert"
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "flex-start",
            gap: 10,
            padding: 18,
            marginTop: 24,
            maxWidth: 520,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--warn)" }}>
            <AlertTriangle size={16} />
            <span style={{ fontWeight: 600, fontSize: 14 }}>This view failed to load.</span>
          </div>
          <div className="muted" style={{ fontSize: 12.5, wordBreak: "break-word" }}>
            {this.state.error.message || String(this.state.error)}
            {explain}
          </div>
          {/* hardReset, not location.reload(). A plain reload is a navigation:
              it goes back through the service worker and the HTTP cache and can
              re-serve the very build whose chunks just 404'd — which made this
              button appear to do nothing at all in the installed app. This one
              purges the worker's caches and cache-busts the document first. */}
          <button className="btn" onClick={() => void hardReset()}>
            <RotateCcw size={13} /> Reload
          </button>
        </div>
      </div>
    );
  }
}
