import { useCallback, useEffect, useState } from "react";

// Runs `fn` when `deps` change; state is 'loading' | 'ready' | 'error'. Answers that arrive after unmount/dep change are dropped.
export function useAsync(fn, deps = []) {
  const [state, setState] = useState({ status: "loading", data: null, error: null });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    setState((s) => ({ status: "loading", data: s.data, error: null }));
    Promise.resolve()
      .then(fn)
      .then(
        (data) => alive && setState({ status: "ready", data, error: null }),
        (error) => alive && setState({ status: "error", data: null, error }),
      );
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}
