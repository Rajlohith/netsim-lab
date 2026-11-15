// Tiny hash router (no extra dependency). URLs look like  #/live/exp-0123456789abcdef
export const ROUTES = [
  { name: "overview", path: "/overview", title: "Overview", group: "Workspace" },
  { name: "topology", path: "/topology", title: "Network topology", group: "Workspace" },
  { name: "configure", path: "/configure", title: "Configure experiment", group: "Experiments" },
  { name: "live", path: "/live", title: "Experiment results", group: "Experiments" },
  { name: "compare", path: "/compare", title: "Compare experiments", group: "Experiments" },
  { name: "history", path: "/history", title: "Experiment history", group: "Experiments" },
];
export const NAV_GROUPS = ["Workspace", "Experiments"].map((g) => ({ group: g, items: ROUTES.filter((r) => r.group === g) }));
export const DEFAULT_ROUTE = "/overview";
const ID_ROUTES = new Set(["live", "history"]);

export function parseHash(hash) {
  const raw = (hash || "").replace(/^#/, "") || DEFAULT_ROUTE;
  const [path] = raw.split("?");
  const parts = path.split("/").filter(Boolean);
  const route = ROUTES.find((r) => r.name === parts[0]);
  if (!route) return { name: "not-found", params: {}, path: raw };
  const id = parts[1] ? decodeURIComponent(parts[1]) : null;
  if (parts.length > 2 || (id && !ID_ROUTES.has(route.name))) return { name: "not-found", params: {}, path: raw };
  return { name: route.name, params: id ? { id } : {}, path: `/${parts.join("/")}` };
}

export const hrefFor = (name, id) => `#/${name}${id ? `/${encodeURIComponent(id)}` : ""}`;
export function navigate(name, id, win = globalThis.window) {
  win.location.hash = hrefFor(name, id).slice(1);
}
