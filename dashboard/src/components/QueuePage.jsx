import React, { useEffect, useState } from 'react';
import {
  Plus, Square, ArrowRight, ExternalLink, FileVideo, Loader2, CheckCircle2, AlertTriangle, Clock, ListOrdered,
} from 'lucide-react';
import { apiJson } from '../lib/api';
import {
  isActive, jobTitle, jobLink, jobProgress, settingChips, youtubeId, when, span,
} from '../lib/jobs';

// Re-render every few seconds so "running 12 min" and ETAs keep moving
// between polls.
function useNow(ms = 5000) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

function Thumb({ job, size = 'md' }) {
  const id = youtubeId(job.label);
  const cls = size === 'lg' ? 'w-full sm:w-56' : 'w-24 sm:w-32';
  return (
    <span className={`${cls} aspect-video shrink-0 rounded-input overflow-hidden border border-rule bg-paper3 flex items-center justify-center`}>
      {id
        ? <img src={`https://i.ytimg.com/vi/${id}/mqdefault.jpg`} alt="" loading="lazy" className="w-full h-full object-cover" />
        : <FileVideo size={size === 'lg' ? 28 : 18} className="text-muted" />}
    </span>
  );
}

function Chips({ job }) {
  return (
    <span className="flex flex-wrap gap-1.5 mt-2">
      {settingChips(job).map((c) => (
        <span key={c} className="px-2 py-0.5 rounded-full border border-rule text-[11px] text-ink2 bg-paper">{c}</span>
      ))}
    </span>
  );
}

function Source({ job }) {
  const link = jobLink(job);
  return (
    <span className="block min-w-0">
      <span className="block text-sm sm:text-[15px] text-ink truncate" title={jobTitle(job)}>{jobTitle(job)}</span>
      {link
        ? (
          <a href={link} target="_blank" rel="noreferrer noopener" onClick={(e) => e.stopPropagation()}
            className="inline-flex items-center gap-1 max-w-full text-xs text-muted hover:text-brass mt-0.5">
            <span className="truncate">{link}</span><ExternalLink size={11} className="shrink-0" />
          </a>
        )
        : job.label && <span className="block text-xs text-muted truncate mt-0.5">uploaded file · {job.label}</span>}
    </span>
  );
}

function StopButton({ job, onStop, label }) {
  if (job.stopping) return <span className="readout">stopping…</span>;
  return (
    <button onClick={() => onStop(job)} className="btn-ghost px-3 py-1.5 text-xs">
      <Square size={12} /> {label}
    </button>
  );
}

function Running({ job, now, onOpen, onStop }) {
  const { stage, total, ready } = jobProgress(job);
  const pct = total ? Math.round((ready / total) * 100) : null;
  return (
    <article className="card p-4 sm:p-5">
      <div className="flex flex-col sm:flex-row gap-4">
        <Thumb job={job} size="lg" />
        <div className="min-w-0 flex-1 flex flex-col">
          <div className="flex items-start justify-between gap-3">
            <Source job={job} />
            <span className="badge-brass shrink-0"><span className="dot dot-ok !bg-brass !shadow-none animate-pulse" /> working</span>
          </div>
          <Chips job={job} />
          <div className="mt-4">
            <div className="flex items-center justify-between gap-3 text-xs">
              <span className="flex items-center gap-2 text-ink2">
                <Loader2 size={13} className="animate-spin text-brass" />
                {stage || 'starting'}
                {total > 0 && <span className="text-muted">· {ready} of {total} clips ready</span>}
              </span>
              {job.started_at && <span className="readout">running {span(now - job.started_at)}</span>}
            </div>
            <div className="mt-2 h-1.5 rounded-full bg-paper3 overflow-hidden">
              {pct == null
                ? <div className="h-full w-1/3 bg-brass/70 rounded-full animate-pulse" />
                : <div className="h-full bg-brass rounded-full transition-all duration-700" style={{ width: `${Math.max(pct, 4)}%` }} />}
            </div>
          </div>
          <div className="mt-4 flex items-center justify-between gap-3">
            <span className="readout">added {when(job.submitted_at)}{job.started_at ? ` · started ${when(job.started_at)}` : ''}</span>
            <span className="flex items-center gap-2">
              <StopButton job={job} onStop={onStop} label="stop" />
              <button onClick={() => onOpen(job)} className="btn-primary px-4 py-1.5 text-xs">
                watch <ArrowRight size={13} />
              </button>
            </span>
          </div>
        </div>
      </div>
    </article>
  );
}

function Waiting({ job, now, onOpen, onStop }) {
  return (
    <li className="flex gap-3 sm:gap-4 p-3 sm:p-4">
      <span className="w-7 h-7 rounded-full border border-rule2 bg-paper flex items-center justify-center text-xs font-mono text-ink2 shrink-0 mt-1">
        {job.position ?? '·'}
      </span>
      <Thumb job={job} />
      <div className="min-w-0 flex-1">
        <Source job={job} />
        <Chips job={job} />
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <span className="readout flex items-center gap-1.5">
            <Clock size={11} />
            {job.eta_seconds != null
              ? <>starts in ~{span(job.eta_seconds)} · around {when(now + job.eta_seconds)}</>
              : 'waiting'}
            <span className="text-muted">· added {when(job.submitted_at)}</span>
          </span>
          <span className="flex items-center gap-2">
            <StopButton job={job} onStop={onStop} label="remove" />
            <button onClick={() => onOpen(job)} className="btn-quiet px-3 py-1.5 text-xs">details</button>
          </span>
        </div>
      </div>
    </li>
  );
}

function Finished({ job, onOpen }) {
  const ok = job.status === 'completed';
  const took = job.started_at && job.finished_at ? span(job.finished_at - job.started_at) : '';
  return (
    <li>
      <button onClick={() => onOpen(job)} className="w-full flex gap-3 sm:gap-4 p-3 sm:p-4 text-left hover:bg-paper2 transition-colors">
        <Thumb job={job} />
        <span className="min-w-0 flex-1">
          <span className="flex items-start justify-between gap-3">
            <Source job={job} />
            {ok
              ? <span className="badge-ok shrink-0"><CheckCircle2 size={11} /> {job.clips} clip{job.clips === 1 ? '' : 's'}</span>
              : <span className="badge-danger shrink-0"><AlertTriangle size={11} /> failed</span>}
          </span>
          <Chips job={job} />
          {!ok && job.error && <span className="block mt-2 text-xs text-danger line-clamp-2 break-words">{job.error}</span>}
          <span className="block readout mt-2">
            {job.finished_at ? `${ok ? 'finished' : 'ended'} ${when(job.finished_at)}` : `added ${when(job.submitted_at)}`}
            {took && ` · took ${took}`}
          </span>
        </span>
        <ArrowRight size={15} className="text-muted shrink-0 self-center" />
      </button>
    </li>
  );
}

// The queue: what runs now, what waits (in the order it will run), what is done.
export default function QueuePage({ jobs, onOpen, onNew, refresh }) {
  const now = useNow();
  const running = (jobs || []).filter((j) => j.status === 'processing');
  const waiting = (jobs || []).filter((j) => j.status === 'queued')
    .sort((a, b) => (a.position ?? 1e9) - (b.position ?? 1e9));
  const done = (jobs || []).filter((j) => !isActive(j));
  const lastEta = waiting.length ? waiting[waiting.length - 1].eta_seconds : null;

  const stop = async (job) => {
    const ask = job.status === 'queued'
      ? 'Remove this video from the queue?'
      : 'Stop this job? Clips it has not finished are lost.';
    if (!window.confirm(ask)) return;
    try {
      await apiJson(`/api/jobs/${job.job_id}/stop`, { method: 'POST' });
    } catch { /* it finished meanwhile: the next poll shows it */ }
    refresh();
  };

  return (
    <div className="h-full overflow-y-auto custom-scrollbar animate-fade">
      <div className="max-w-4xl mx-auto px-4 py-6 sm:px-8 sm:py-8">
        <div className="flex flex-wrap items-end justify-between gap-4 mb-8">
          <div>
            <h2 className="font-display lowercase text-[2rem] sm:text-[2.5rem] leading-none text-ink">queue</h2>
            <p className="text-muted text-sm mt-2">
              {jobs == null ? 'Loading…'
                : running.length + waiting.length === 0
                  ? 'Nothing running. Videos you add run one after another, so you can line up several and come back to the clips.'
                  : <>
                    {running.length} running · {waiting.length} waiting
                    {lastEta != null && <> · the last one starts around <span className="text-ink2">{when(now + lastEta)}</span></>}
                  </>}
            </p>
          </div>
          <button onClick={onNew} className="btn-primary px-4 py-2 text-sm"><Plus size={15} /> add a video</button>
        </div>

        {running.length > 0 && (
          <section className="mb-8" aria-label="running now">
            <p className="eyebrow mb-3">now running</p>
            <div className="space-y-3">
              {running.map((j) => <Running key={j.job_id} job={j} now={now} onOpen={onOpen} onStop={stop} />)}
            </div>
          </section>
        )}

        {waiting.length > 0 && (
          <section className="mb-8" aria-label="up next">
            <p className="eyebrow mb-3">up next · {waiting.length}</p>
            <ol className="card divide-y divide-rule overflow-hidden">
              {waiting.map((j) => <Waiting key={j.job_id} job={j} now={now} onOpen={onOpen} onStop={stop} />)}
            </ol>
          </section>
        )}

        {done.length > 0 && (
          <section aria-label="finished">
            <p className="eyebrow mb-3">finished</p>
            <ul className="card divide-y divide-rule overflow-hidden">
              {done.map((j) => <Finished key={j.job_id} job={j} onOpen={onOpen} />)}
            </ul>
          </section>
        )}

        {jobs && jobs.length === 0 && (
          <div className="card p-10 text-center">
            <ListOrdered size={28} className="text-muted mx-auto" />
            <p className="text-ink mt-4">No videos yet</p>
            <p className="text-muted text-sm mt-1">Add a link or a file; it shows up here with its progress.</p>
          </div>
        )}
      </div>
    </div>
  );
}
