import React from 'react';
import { Bot } from 'lucide-react';
import { tokens, totalTokens } from '../lib/jobs';

const TASK_LABEL = {
  clips: 'pick moments',
  visual_clips: 'pick moments (silent)',
  layout: 'layout',
  hooks: 'hooks',
  subtitles: 'subtitle check',
  edits: 'auto edit',
  studio: 'youtube studio',
  pro_mode: 'pro mode',
  other: 'other',
};

// Claude usage of a job or a period: totals, then each task with its share.
export default function UsageCard({ usage, title = 'claude usage', compact = false }) {
  if (!usage || !usage.calls) {
    return compact ? null : (
      <div className="rounded-input border border-rule bg-paper px-4 py-3 text-xs text-muted">
        No Claude calls recorded yet.
      </div>
    );
  }
  const tasks = Object.entries(usage.tasks || {}).sort((a, b) => totalTokens(b[1]) - totalTokens(a[1]));
  const max = Math.max(...tasks.map(([, t]) => totalTokens(t)), 1);
  return (
    <section className="rounded-input border border-rule bg-paper px-4 py-3" aria-label={title}>
      <div className="flex items-center justify-between gap-3">
        <p className="readout flex items-center gap-1.5"><Bot size={12} className="text-brass" /> {title}</p>
        <p className="text-xs text-ink2 tabular-nums">
          <span className="text-ink">{tokens(totalTokens(usage))}</span> tokens · {usage.calls} call{usage.calls === 1 ? '' : 's'}
        </p>
      </div>
      <p className="text-[11px] text-muted mt-1 tabular-nums">
        {tokens(usage.input_tokens)} in · {tokens(usage.output_tokens)} out · {Math.round(usage.seconds || 0)} s waiting on Claude
      </p>
      {!compact && (
        <ul className="mt-3 space-y-2">
          {tasks.map(([id, t]) => (
            <li key={id}>
              <div className="flex items-center justify-between gap-2 text-xs">
                <span className="text-ink2 truncate">
                  {TASK_LABEL[id] || id}
                  <span className="text-muted"> · {Object.keys(t.models || {}).join(', ')}</span>
                  {t.failed > 0 && <span className="text-danger"> · {t.failed} failed</span>}
                </span>
                <span className="tabular-nums text-muted shrink-0">{tokens(totalTokens(t))} · {t.calls}×</span>
              </div>
              <div className="mt-1 h-1 rounded-full bg-paper3 overflow-hidden">
                <div className="h-full bg-brass/80 rounded-full" style={{ width: `${Math.max(2, (totalTokens(t) / max) * 100)}%` }} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
