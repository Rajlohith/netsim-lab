import { useEffect, useRef, useState } from "react";
import { createPoller } from "../lib/poll.js";
import { TERMINAL } from "../lib/experimentActions.js";

// Polls GET /experiments/{id} (+ /transitions?after_seq=N) until the experiment reaches a terminal status.
// The backend has no WebSocket and no metric stream, so polling the lifecycle is the only live information there is.
export function usePolledExperiment(api, id, { intervalMs = 1000 } = {}) {
  const [state, setState] = useState({ experiment: null, transitions: [], loading: true, pollError: null, failures: 0, lastUpdated: null });
  const seq = useRef(0);
  const pollerRef = useRef(null);

  useEffect(() => {
    if (!id) return undefined;
    seq.current = 0;
    setState({ experiment: null, transitions: [], loading: true, pollError: null, failures: 0, lastUpdated: null });
    const poller = createPoller({
      intervalMs,
      fn: async () => {
        const experiment = await api.getExperiment(id);
        const tr = await api.transitions(id, seq.current);
        return { experiment, tr };
      },
      shouldStop: ({ experiment }) => TERMINAL.has(experiment.status),
      onResult: ({ experiment, tr }) => {
        if (tr.next_after_seq != null) seq.current = tr.next_after_seq;
        setState((s) => ({ experiment, transitions: [...s.transitions, ...(tr.items || [])], loading: false, pollError: null, failures: 0, lastUpdated: new Date().toISOString() }));
      },
      onError: (e, failures) => setState((s) => ({ ...s, loading: false, pollError: e, failures })),
    });
    pollerRef.current = poller;
    poller.start();
    return () => poller.stop();
  }, [api, id, intervalMs]);

  const refresh = () => { if (pollerRef.current) { pollerRef.current.stop(); pollerRef.current.start(); } };
  return { ...state, refresh };
}
