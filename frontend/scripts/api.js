// Backend client. The backend is the only source of truth for analytical and verification state.

export class ConnectionError extends Error {}
export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function request(path, { method = "GET", body } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json", Accept: "application/json" } : { Accept: "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ConnectionError("The analytical service is currently unavailable.");
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (!res.ok) throw new ApiError(data?.error || `Request failed (${res.status}).`, res.status);
  return data;
}

const enc = encodeURIComponent;

export const api = {
  status: () => request("/api/status"),
  datasets: () => request("/api/datasets"),
  dataset: (name) => request(`/api/datasets/${enc(name)}`),
  examples: () => request("/api/examples"),
  history: () => request("/api/history"),
  analyze: (question, claim) => request("/api/analyze", { method: "POST", body: { question, claim } }),
  analysis: (id) => request(`/api/analysis/${enc(id)}`),
  rerun: (id) => request(`/api/analysis/${enc(id)}/rerun`, { method: "POST", body: {} }),
  benchmark: () => request("/api/benchmark"),
  runBenchmark: () => request("/api/benchmark", { method: "POST", body: {} }),
  upload: (files) => request("/api/workspace", { method: "POST", body: { files } }),
  useDemo: () => request("/api/workspace", { method: "POST", body: { demo: true } }),
  useUploads: () => request("/api/workspace", { method: "POST", body: { uploaded: true } }),
  model: () => request("/api/model"),
  setMode: (mode) => request("/api/mode", { method: "POST", body: { mode } }),
  installModel: () => request("/api/model/install", { method: "POST", body: {} }),
  useBoth: () => request("/api/workspace", { method: "POST", body: { both: true } }),
  removeUpload: (name) => request(`/api/uploads/${enc(name)}`, { method: "DELETE" }),
  exportUrl: (id) => `/api/analysis/${enc(id)}/export`,
  bundleUrl: (id) => `/api/analysis/${enc(id)}/bundle`,
  verifyCode: (id, code) => request(`/api/analysis/${enc(id)}/verify`, { method: "POST", body: code == null ? {} : { code } }),
  runCode: (code, tables) => request("/api/run", { method: "POST", body: { code, tables } }),
  benchmarkProgress: () => request("/api/benchmark/progress"),
  cancelBenchmark: () => request("/api/benchmark/cancel", { method: "POST", body: {} }),
  fixes: () => request("/api/fixes"),
  previewFix: (fix_id, choice, value) => request("/api/fixes/preview", { method: "POST", body: { fix_id, choice, value } }),
  applyFix: (fix_id, choice, value, token) => request("/api/fixes/apply", { method: "POST", body: { fix_id, choice, value, token } }),
  rollbackFix: (entry_id) => request("/api/fixes/rollback", { method: "POST", body: { entry_id } }),
  restoreTable: (table) => request("/api/fixes/restore", { method: "POST", body: { table } }),
};

/** Live analysis updates via Server-Sent Events. Returns a function that stops listening. */
export function streamAnalysis(id, { onState, onEnd, onError }) {
  const es = new EventSource(`/api/analysis/${enc(id)}/events`);
  let ended = false;
  es.addEventListener("state", (e) => onState(JSON.parse(e.data)));
  es.addEventListener("end", () => { ended = true; es.close(); onEnd?.(); });
  es.onerror = () => { if (!ended) { es.close(); onError?.(); } };
  return () => es.close();
}

export async function filesToPayload(fileList) {
  const read = (file) => new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve({ name: file.name, data_base64: String(r.result).split(",")[1] || "" });
    r.onerror = () => reject(new Error(`Could not read ${file.name}.`));
    r.readAsDataURL(file);
  });
  return Promise.all([...fileList].map(read));
}

/* the three workspaces, as named in the interface */
export const WORKSPACES = { demonstration: "Demonstration data", uploaded: "Your uploads", both: "Demonstration + uploads" };
