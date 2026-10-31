// Polling controller (the backend has no WebSocket: GET /api/capabilities -> live_updates.websocket === false).
// Scheduler functions are injectable so the behaviour is testable without real timers.
export function createPoller({ fn, intervalMs = 1000, maxBackoffMs = 10000, shouldStop = () => false, onResult = () => {}, onError = () => {},
  schedule = (f, ms) => setTimeout(f, ms), cancel = (h) => clearTimeout(h) }) {
  let generation = 0, handle = null, failures = 0, running = false;

  async function tick(gen) {
    if (gen !== generation) return;
    try {
      const result = await fn();
      if (gen !== generation) return;       // stopped while the request was in flight: drop the answer
      failures = 0;
      onResult(result);
      if (shouldStop(result)) { running = false; return; }
      handle = schedule(() => tick(gen), intervalMs);
    } catch (e) {
      if (gen !== generation) return;
      failures += 1;
      onError(e, failures);
      handle = schedule(() => tick(gen), Math.min(maxBackoffMs, intervalMs * 2 ** Math.min(failures, 8)));
    }
  }
  return {
    start() { if (running) return; running = true; generation += 1; tick(generation); },
    stop() { running = false; generation += 1; if (handle !== null) cancel(handle); handle = null; },
    get running() { return running; },
  };
}
