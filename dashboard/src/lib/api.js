// Small fetch helpers around the backend API.
import { getApiUrl } from '../config';

export async function apiFetch(path, options = {}) {
  return fetch(getApiUrl(path), options);
}

// A failed request, carrying the server's own explanation. Callers that show an
// error should prefer `detail`: FastAPI's message is usually the actionable one
// ("this video is only 30 s long").
export class ApiError extends Error {
  constructor(status, detail, raw) {
    super(`${status}: ${raw}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

export async function apiJson(path, options = {}) {
  const res = await apiFetch(path, options);
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    let detail = '';
    try {
      const d = JSON.parse(text).detail;
      // FastAPI details are strings here, but validation errors arrive as objects.
      if (typeof d === 'string') detail = d;
    } catch (_) { /* not JSON: the caller's own copy is used */ }
    throw new ApiError(res.status, detail, text);
  }
  return res.json();
}
