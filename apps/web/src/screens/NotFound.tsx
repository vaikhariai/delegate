import { navigate } from "../lib";

export function NotFound() {
  return (
    <main id="main" className="page page-eval" data-testid="not-found-page">
      <h1>Page not found</h1>
      <p className="lede">There is nothing at this address. Your demo scenario, if you started one, is still saved in this tab.</p>
      <p>
        <button className="btn btn-primary" data-testid="not-found-home" onClick={() => navigate("/")}>
          Go to the start
        </button>
      </p>
    </main>
  );
}
