import { useEffect, useState } from "react";

export default function App() {
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch("/api/health")
      .then((r) => r.json())
      .then(setHealth)
      .catch((e) => setError(String(e)));
  }, []);

  return (
    <main>
      <h1>NetSim Lab</h1>
      <p>Backend: {error ? `unreachable (${error})` : health ? health.status : "checking…"}</p>
    </main>
  );
}
