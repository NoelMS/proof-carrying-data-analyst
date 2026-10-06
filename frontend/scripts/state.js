// Minimal application store. Views render from it; nothing here decides analytical truth.

export const store = {
  status: null,        // GET /api/status
  datasets: null,      // GET /api/datasets
  history: null,       // GET /api/history
  connection: "connecting", // connecting | ok | lost
  activity: null,      // { label, busy } derived from the analysis currently streaming
};

const listeners = new Set();

export function update(patch) {
  Object.assign(store, patch);
  listeners.forEach((fn) => fn(store));
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
