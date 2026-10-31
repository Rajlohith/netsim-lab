import React, { useEffect, useState } from "react";
import { ApiProvider, useApi } from "./hooks/api.jsx";
import { api as defaultApi } from "./api/endpoints.js";
import { DEFAULT_ROUTE, ROUTES, hrefFor, parseHash } from "./lib/router.js";
import TopologyPage from "./pages/TopologyPage.jsx";
import ConfigurePage from "./pages/ConfigurePage.jsx";
import LivePage from "./pages/LivePage.jsx";
import PredictionsPage from "./pages/PredictionsPage.jsx";
import ComparePage from "./pages/ComparePage.jsx";
import ModelsPage from "./pages/ModelsPage.jsx";
import HistoryPage from "./pages/HistoryPage.jsx";
import { EmptyState } from "./components/StateViews.jsx";

export function Nav({ current }) {
  return (
    <nav aria-label="Main navigation" className="nav">
      <ul>{ROUTES.map((r) => <li key={r.name}><a href={hrefFor(r.name)} aria-current={current === r.name ? "page" : undefined}>{r.title}</a></li>)}</ul>
    </nav>
  );
}

export function Page({ route }) {
  const id = route.params.id;
  switch (route.name) {
    case "topology": return <TopologyPage />;
    case "configure": return <ConfigurePage />;
    case "live": return <LivePage id={id} />;
    case "predictions": return <PredictionsPage id={id} />;
    case "compare": return <ComparePage />;
    case "models": return <ModelsPage />;
    case "history": return <HistoryPage id={id} />;
    default: return <EmptyState title="Page not found">There is no page at <code>#{route.path}</code>. Use the navigation.</EmptyState>;
  }
}

function Backend() {
  const api = useApi();
  const [s, setS] = useState({ state: "checking" });
  useEffect(() => { let alive = true; api.health().then((h) => alive && setS({ state: "ok", detail: h.status }), (e) => alive && setS({ state: "down", detail: e.message })); return () => { alive = false; }; }, [api]);
  return <span className={`backend backend-${s.state}`} role="status">API: {s.state === "ok" ? "reachable" : s.state === "down" ? "unreachable" : "checking…"}</span>;
}

export default function App({ api = defaultApi, initialHash }) {
  const [hash, setHash] = useState(initialHash ?? (typeof window !== "undefined" ? window.location.hash : ""));
  useEffect(() => {
    if (initialHash !== undefined || typeof window === "undefined") return undefined;
    const on = () => setHash(window.location.hash);
    window.addEventListener("hashchange", on);
    if (!window.location.hash) window.location.hash = DEFAULT_ROUTE;
    return () => window.removeEventListener("hashchange", on);
  }, [initialHash]);
  const route = parseHash(hash);
  const title = (ROUTES.find((r) => r.name === route.name) || {}).title || "Not found";
  useEffect(() => { if (typeof document !== "undefined") document.title = `${title} · NetSim Lab`; }, [title]);
  return (
    <ApiProvider api={api}>
      <div className="app">
        <header className="topbar"><span className="brand">NetSim Lab</span><span className="tag">mobility experiments · all data simulated or loopback, never a live network</span><Backend /></header>
        <Nav current={route.name} />
        <main id="main" key={`${route.name}/${route.params.id || ""}`}><Page route={route} /></main>
      </div>
    </ApiProvider>
  );
}
