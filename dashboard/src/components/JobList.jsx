import React from 'react';
import { ChevronRight, ArrowRight } from 'lucide-react';
import { isActive, jobTitle, jobProgress, settingChips, when, span } from '../lib/jobs';

const BADGE = {
  queued: 'badge-warn',
  processing: 'badge-brass',
  completed: 'badge-ok',
  failed: 'badge-danger',
};

function statusText(job) {
  if (job.status === 'queued') return job.position ? `queued #${job.position}` : 'queued';
  if (job.status === 'processing') return jobProgress(job).stage || 'working';
  if (job.status === 'completed') return `${job.clips} clip${job.clips === 1 ? '' : 's'}`;
  return 'failed';
}

// The queue at a glance under the new-video form: what runs and waits, then
// the latest finished ones; the queue page has the rest.
export default function JobList({ jobs, onOpen, onViewAll }) {
  if (!jobs || !jobs.length) return null;
  // Run order: the running job, then the queue by position.
  const active = jobs.filter(isActive).sort((a, b) =>
    (a.status === 'processing' ? -1 : a.position ?? 1e9) - (b.status === 'processing' ? -1 : b.position ?? 1e9));
  const shown = [...active, ...jobs.filter((j) => !isActive(j)).slice(0, Math.max(0, 4 - active.length))];

  return (
    <section className="mt-10" aria-label="your jobs">
      <div className="flex items-center justify-between mb-3">
        <p className="eyebrow">your queue</p>
        <button onClick={onViewAll} className="text-xs text-muted hover:text-brass inline-flex items-center gap-1">
          open queue{jobs.length > shown.length ? ` · ${jobs.length}` : ''} <ArrowRight size={12} />
        </button>
      </div>
      <ul className="card divide-y divide-rule overflow-hidden">
        {shown.map((job) => (
          <li key={job.job_id}>
            <button onClick={() => onOpen(job)}
              className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-paper2 transition-colors">
              <span className="min-w-0 flex-1">
                <span className="block text-sm text-ink truncate">{jobTitle(job)}</span>
                <span className="block readout mt-0.5 truncate">
                  {settingChips(job).slice(0, 3).join(' · ')}
                  {job.status === 'queued' && job.eta_seconds != null && ` · starts in ~${span(job.eta_seconds)}`}
                  {!isActive(job) && ` · ${when(job.finished_at || job.submitted_at)}`}
                </span>
              </span>
              <span className={`${BADGE[job.status] || 'badge-warn'} shrink-0`}>{statusText(job)}</span>
              <ChevronRight size={15} className="text-muted shrink-0" />
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
