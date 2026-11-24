/**
 * The whole app behind an error boundary. Without one, an error while drawing
 * any screen unmounts the app and leaves a blank white page that only killing
 * the app clears (the native side also reloads a page that comes back from
 * the background with nothing drawn: OlanoBridgeViewController.swift). With
 * it, the person sees what happened, with the error's own words to pass on,
 * and a button that loads the app again (the session survives a reload).
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface State {
  error: Error | null;
}

export class CrashBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: unknown): State {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    console.error("The app stopped drawing:", error, info.componentStack);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="olano-crash" role="alert">
        <h1>Something went wrong</h1>
        <p>The app hit a problem showing this screen. Reload to carry on; you stay signed in.</p>
        <button type="button" onClick={() => window.location.reload()}>
          Reload
        </button>
        <pre>{error.message || error.name}</pre>
      </div>
    );
  }
}
