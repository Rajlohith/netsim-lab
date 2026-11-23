import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to the backend. NETSIM_API_HOST / NETSIM_API_PORT are the same variables the API itself uses.
const apiTarget = `http://${process.env.NETSIM_API_HOST || "127.0.0.1"}:${process.env.NETSIM_API_PORT || "8000"}`;

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": apiTarget } },
  test: { environment: "jsdom" },
});
