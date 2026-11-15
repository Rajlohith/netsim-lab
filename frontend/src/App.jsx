import React, { useEffect, useState } from "react";
import { ApiProvider, useApi } from "./hooks/api.jsx";
import { api as defaultApi } from "./api/endpoints.js";
import { DEFAULT_ROUTE, NAV_GROUPS, ROUTES, hrefFor, parseHash } from "./lib/router.js";
import { useTheme } from "./hooks/useTheme.js";
import OverviewPage from "./pages/OverviewPage.jsx";
import TopologyPage from "./pages/TopologyPage.jsx";
import ConfigurePage from "./pages/ConfigurePage.jsx";
import LivePage from "./pages/LivePage.jsx";
import ComparePage from "./pages/ComparePage.jsx";
import HistoryPage from "./pages/HistoryPage.jsx";
import { EmptyState } from "./components/StateViews.jsx";

export function Nav({ current, onNavigate }) {
  return (
    <nav aria-label="Main navigation" className="nav">
      {NAV_GROUPS.map((g) => (
        <div className="nav-group" key={g.group}>
          <p className="nav-group-title">{g.group}</p>
          <ul>{g.items.map((r) => <li key={r.name}><a href={hrefFor(r.name)} onClick={onNavigate} aria-current={current === r.name ? "page" : undefined}>{r.title}</a></li>)}</ul>
        </div>
      ))}
    </nav>
  );
}

export function ThemeToggle({ theme, onToggle }) {
  const dark = theme === "dark";
  return (
    <button type="button" className="icon-btn" onClick={onToggle} aria-pressed={dark} aria-label={dark ? "Switch to light theme" : "Switch to dark theme"} title={dark ? "Switch to light theme" : "Switch to dark theme"}>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        {dark ? <><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></> : <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />}
      </svg>
    </button>
  );
}

export function Page({ route }) {
  const id = route.params.id;
  switch (route.name) {
    case "overview": return <OverviewPage />;
    case "topology": return <TopologyPage />;
    case "configure": return <ConfigurePage />;
    case "live": return <LivePage id={id} />;
    case "compare": return <ComparePage />;
    case "history": return <HistoryPage id={id} />;
    default: return <EmptyState title="Page not found">There is no page at <code>#{route.path}</code>. Use the navigation.</EmptyState>;
  }
}

function Backend() {
  const api = useApi();
  const [s, setS] = useState({ state: "checking" });
  useEffect(() => { let alive = true; api.health().then((h) => alive && setS({ state: "ok", detail: h.status }), (e) => alive && setS({ state: "down", detail: e.message })); return () => { alive = false; }; }, [api]);
  return <span className={`backend backend-${s.state}`} role="status">API {s.state === "ok" ? "reachable" : s.state === "down" ? "unreachable" : "checking…"}</span>;
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
  const { theme, toggle } = useTheme();
  const [menuOpen, setMenuOpen] = useState(false);
  return (
    <ApiProvider api={api}>
      <div className="app">
        <aside className={`sidebar${menuOpen ? " open" : ""}`}>
          <a className="brand" href={hrefFor("overview")} onClick={() => setMenuOpen(false)}><span className="brand-mark" aria-hidden="true">N</span>NetSim Lab</a>
          <Nav current={route.name} onNavigate={() => setMenuOpen(false)} />
          <p className="nav-foot">Simulated and loopback data only</p>
        </aside>
        {menuOpen ? <div className="scrim" onClick={() => setMenuOpen(false)} aria-hidden="true" /> : null}
        <div className="shell">
          <header className="topbar">
            <button type="button" className="icon-btn menu-btn" aria-label="Open navigation" aria-expanded={menuOpen} onClick={() => setMenuOpen((o) => !o)}>☰</button>
            <span className="crumb">{title}</span>
            <Backend />
            <ThemeToggle theme={theme} onToggle={toggle} />
          </header>
          <main id="main" key={`${route.name}/${route.params.id || ""}`}><Page route={route} /></main>
        </div>
      </div>
    </ApiProvider>
  );
}
