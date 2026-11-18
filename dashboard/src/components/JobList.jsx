import React, { useEffect, useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { apiJson } from '../lib/api';

const BADGE = {
  queued: ['badge-warn', 'queued'],
  processing: ['badge-brass', 'working'],
  completed: ['badge-ok', 'done'],
  failed: ['badge-danger', 'failed'],
};

const when = (epoch) => {
  if (!epoch) return '';
  const d = new Date(epoch * 1000);
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
};

// Every job the server holds, so queued, running and finished ones stay
// reachable after "new video". Renders nothing where /api/jobs is not served
// (the cloud) or while the server holds no job.
export default function JobList({ onOpen }) {
  const [jobs, setJobs] = useState([]);

  useEffect(() => {
    let alive = true;
    const load = () => apiJson('/api/jobs')
      .then((d) => { if (alive) setJobs(d.jobs || []); })
      .catch(() => { /* not offered here, or the server is restarting */ });
    load();
    const t = setInterval(load, 5000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  if (!jobs.length) return null;

  return (
    <section className="mt-10" aria-label="your jobs">
      <p className="eyebrow mb-3">your jobs</p>
      <ul className="card divide-y divide-rule overflow-hidden">
        {jobs.map((job) => {
          const [badge, label] = BADGE[job.status] || ['badge-warn', job.status];
          return (
            <li key={job.job_id}>
              <button onClick={() => onOpen(job)}
                className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-paper2 transition-colors">
                <span className="min-w-0 flex-1">
                  <span className="block text-sm text-ink truncate">{job.label || `job ${job.job_id.slice(0, 8)}`}</span>
                  <span className="block readout mt-0.5">
                    {when(job.submitted_at)}
                    {job.status === 'completed' && ` · ${job.clips} clip${job.clips === 1 ? '' : 's'}`}
                  </span>
                </span>
                <span className={`${badge} shrink-0`}>
                  {label}{job.status === 'queued' && job.position ? ` #${job.position}` : ''}
                </span>
                <ChevronRight size={15} className="text-muted shrink-0" />
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
