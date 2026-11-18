// What the backend reports about itself (/api/config): the Claude model in use,
// how long finished jobs are kept, whether YouTube links are accepted. Fetched
// once per page load and shared by every caller.
import { useEffect, useState } from 'react';
import { getApiUrl } from '../config';

let cached = null;
let inflight = null;

function load() {
  if (!inflight) {
    inflight = fetch(getApiUrl('/api/config'))
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((cfg) => { cached = { ...cfg, online: true }; return cached; })
      .catch(() => { inflight = null; return { online: false }; });
  }
  return inflight;
}

export function useServerConfig() {
  const [config, setConfig] = useState(cached || { online: null });
  useEffect(() => {
    let alive = true;
    if (!cached) load().then((c) => { if (alive) setConfig(c); });
    return () => { alive = false; };
  }, []);
  return config;
}
