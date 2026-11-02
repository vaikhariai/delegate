import { Component, type ReactNode } from "react";

interface State {
  failed: boolean;
}

/** Last-resort screen if rendering throws. Server state is untouched, so reloading recovers the scenario. */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <main id="main" className="page page-eval" data-testid="crash-page" role="alert">
        <h1>Something went wrong on this page</h1>
        <p className="lede">
          Nothing was sent or reserved because of this error: every action is decided on the server. Reload to continue where
          you left off.
        </p>
        <p>
          <button className="btn btn-primary" onClick={() => window.location.reload()}>
            Reload
          </button>
        </p>
      </main>
    );
  }
}
