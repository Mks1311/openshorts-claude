// The server's job list (GET /api/jobs) and how to read one job of it.
import { useEffect, useState } from 'react';
import { apiJson } from './api';
import { clipProgress, jobStage } from './clipProgress';

// Every job the server holds, newest first, refreshed every 3 s. null until the
// first answer, and stays null where /api/jobs is not served (the cloud).
export function useJobs() {
  const [jobs, setJobs] = useState(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    const load = () => apiJson('/api/jobs')
      .then((d) => { if (alive) setJobs(d.jobs || []); })
      .catch((e) => { if (e?.status === 404) clearInterval(t); /* else restarting: retry */ });
    const t = setInterval(load, 3000);
    load();
    return () => { alive = false; clearInterval(t); };
  }, [tick]);
  return { jobs, refresh: () => setTick((n) => n + 1) };
}

export const isActive = (job) => job.status === 'queued' || job.status === 'processing';

// h:mm:ss or m:ss, as the form takes them.
export function clock(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return '';
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

// "12 min", "1 h 05 min".
export function span(seconds) {
  if (seconds == null || seconds < 0) return '';
  const m = Math.max(1, Math.round(seconds / 60));
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`;
}

export function when(epoch) {
  if (!epoch) return '';
  const d = new Date(epoch * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return d.toLocaleString([], today
    ? { hour: '2-digit', minute: '2-digit' }
    : { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

const isLink = (s) => /^https?:\/\//i.test(s || '');

export function youtubeId(url) {
  if (!isLink(url)) return null;
  try {
    const u = new URL(url);
    if (u.hostname.endsWith('youtu.be')) return u.pathname.slice(1, 12) || null;
    if (u.hostname.includes('youtube.com')) {
      return u.searchParams.get('v') || u.pathname.match(/\/(?:shorts|live|embed)\/([\w-]{11})/)?.[1] || null;
    }
  } catch { /* not a URL */ }
  return null;
}

// A file name made readable: no extension or yt-dlp format suffix, spaces back.
const pretty = (name) => name
  .replace(/\.f\d+(?=\.\w+$)/, '').replace(/\.\w{2,4}$/, '').replace(/_+/g, ' ').trim();

// The name to show: the video's own title once it is known, else the link or file.
export function jobTitle(job) {
  if (job.title) return pretty(job.title);
  const dest = (job.logs || []).map((l) => /Destination: .*\/([^/]+)$/.exec(String(l))?.[1]).filter(Boolean).pop();
  if (dest) return pretty(dest);
  if (job.label && !isLink(job.label)) return pretty(job.label);
  if (isLink(job.label)) {
    try {
      const u = new URL(job.label);
      return youtubeId(job.label) ? 'YouTube video' : u.hostname.replace(/^www\./, '') + u.pathname;
    } catch { /* fall through */ }
  }
  return `job ${job.job_id.slice(0, 8)}`;
}

export const jobLink = (job) => (isLink(job.label) ? job.label : null);

// Where a running job stands: its stage and clips rendered so far.
export function jobProgress(job) {
  return { stage: jobStage(job.logs), ...clipProgress(job.logs) };
}

const FORMAT = { vertical: '9:16', square: '1:1', horizontal: '16:9', auto: 'auto format' };

// The options the job was started with, as short labels.
export function settingChips(job) {
  const s = job.settings;
  const chips = [];
  chips.push(job.range ? `${clock(job.range.start)} → ${clock(job.range.end)}` : 'whole video');
  if (!s) return chips;
  if (s.format) chips.push(FORMAT[s.format] || s.format);
  chips.push(s.clips ? `${s.clips} clip${s.clips === 1 ? '' : 's'}` : 'auto clip count');
  if (s.min_seconds || s.max_seconds) chips.push(`${s.min_seconds ?? 15}–${s.max_seconds ?? 60} s`);
  for (const l of s.layouts || []) chips.push(l === 'auto' ? 'auto layout' : l === 'none' ? 'single crop' : l.replace(/_/g, ' '));
  chips.push(s.hook ? `hook · ${s.hook}` : 'no hook');
  if (s.captions === false) chips.push('no captions');
  return chips;
}

// 1234 -> "1.2k", 2500000 -> "2.5M".
export const tokens = (n) => {
  const v = Number(n) || 0;
  if (v >= 1e6) return `${(v / 1e6).toFixed(v >= 1e7 ? 0 : 1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(v >= 1e4 ? 0 : 1)}k`;
  return String(v);
};

export const totalTokens = (u) => (u ? (u.input_tokens || 0) + (u.output_tokens || 0) : 0);

// Delete a finished, failed or stopped job and its files, after asking.
// Returns true when it is gone.
export async function deleteJob(job) {
  if (!window.confirm('Delete this job? Its clips and files on this machine are removed for good.')) return false;
  try {
    await apiJson(`/api/jobs/${job.job_id}`, { method: 'DELETE' });
    return true;
  } catch (e) {
    window.alert(e.detail || 'Could not delete this job.');
    return false;
  }
}
