import { useCallback, useEffect, useState } from "react";

export const THEME_KEY = "netsim-theme";

// Reads the stored choice; falls back to the OS preference only for the first visit. Storage can be blocked, so every access is guarded.
export function readTheme(win = globalThis.window) {
  try {
    const v = win.localStorage.getItem(THEME_KEY);
    if (v === "light" || v === "dark") return v;
  } catch { /* storage unavailable */ }
  try { return win.matchMedia && win.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"; } catch { return "light"; }
}

export function applyTheme(theme, doc = globalThis.document) {
  if (doc && doc.documentElement) { doc.documentElement.dataset.theme = theme; doc.documentElement.style.colorScheme = theme; }
}

export function useTheme() {
  const [theme, setTheme] = useState(() => (typeof window === "undefined" ? "light" : readTheme()));
  useEffect(() => { applyTheme(theme); }, [theme]);
  const toggle = useCallback(() => {
    setTheme((t) => {
      const next = t === "dark" ? "light" : "dark";
      try { window.localStorage.setItem(THEME_KEY, next); } catch { /* keep the choice for this session only */ }
      return next;
    });
  }, []);
  return { theme, toggle };
}
