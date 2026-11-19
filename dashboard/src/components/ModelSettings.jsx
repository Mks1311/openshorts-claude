import React, { useEffect, useState } from 'react';
import { Loader2, RotateCcw } from 'lucide-react';
import { apiJson } from '../lib/api';
import UsageCard from './UsageCard';
import { tokens } from '../lib/jobs';

const MODEL_HINT = {
  haiku: 'fastest, lightest on your limit',
  sonnet: 'balanced',
  opus: 'strongest, heaviest on your limit',
};

// One model per kind of Claude call; saved on the server, used from the next call on.
export function ModelPicker() {
  const [data, setData] = useState(null);
  const [saving, setSaving] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    apiJson('/api/settings/models').then(setData).catch(() => setData(false));
  }, []);

  const pick = async (task, model) => {
    setSaving(task);
    setError('');
    try {
      setData(await apiJson('/api/settings/models', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ models: { [task]: model || null } }),
      }));
    } catch (e) {
      setError(e.detail || 'Could not save that model.');
    } finally {
      setSaving('');
    }
  };

  if (data === false) return <p className="text-sm text-muted">Model choice is not available on this server.</p>;
  if (!data) return <p className="text-sm text-muted flex items-center gap-2"><Loader2 size={13} className="animate-spin" /> loading…</p>;

  return (
    <div>
      <ul className="divide-y divide-rule rounded-input border border-rule bg-paper">
        {data.tasks.map((t) => (
          <li key={t.id} className="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-4 px-4 py-3">
            <span className="min-w-0 flex-1">
              <span className="block text-sm text-ink">{t.label}</span>
              <span className="block text-xs text-muted mt-0.5">{t.help}</span>
            </span>
            <span className="flex items-center gap-2 shrink-0">
              {saving === t.id && <Loader2 size={13} className="animate-spin text-brass" />}
              {t.picked && (
                <button type="button" onClick={() => pick(t.id, null)} title={`back to ${t.default}`}
                  className="p-1 text-muted hover:text-ink" aria-label={`reset ${t.label} to ${t.default}`}>
                  <RotateCcw size={13} />
                </button>
              )}
              <select value={t.picked || ''} onChange={(e) => pick(t.id, e.target.value)} disabled={!!saving}
                aria-label={`model for ${t.label}`}
                className="input-field py-1.5 px-2.5 text-sm w-full sm:w-52">
                <option value="">default · {t.picked ? t.default : t.model}</option>
                {data.choices.map((m) => (
                  <option key={m} value={m}>{m}{MODEL_HINT[m] ? ` · ${MODEL_HINT[m]}` : ''}</option>
                ))}
              </select>
            </span>
          </li>
        ))}
      </ul>
      {error && <p className="text-xs text-danger mt-2">{error}</p>}
      <p className="text-xs text-muted mt-3 leading-relaxed">
        Bigger models pick better moments and write better hooks, and use more of your Claude plan&apos;s
        limit. A change applies from the next call, including jobs already running.
      </p>
    </div>
  );
}

// Claude usage on this server over the last days.
export function UsageSummary() {
  const [days, setDays] = useState(7);
  const [data, setData] = useState(null);

  useEffect(() => {
    let alive = true;
    apiJson(`/api/usage?days=${days}`).then((d) => alive && setData(d)).catch(() => alive && setData(false));
    return () => { alive = false; };
  }, [days]);

  if (data === false) return <p className="text-sm text-muted">Usage is not available on this server.</p>;
  const peak = Math.max(...(data?.days || []).map((d) => d.input_tokens + d.output_tokens), 1);

  return (
    <div className="space-y-3">
      <div className="flex gap-1">
        {[1, 7, 30].map((d) => (
          <button key={d} type="button" onClick={() => setDays(d)}
            className={`px-2.5 py-0.5 rounded-full text-[11px] border ${days === d ? 'border-brass text-ink bg-brass/10' : 'border-rule text-muted hover:text-ink2'}`}>
            {d === 1 ? 'today' : `${d} days`}
          </button>
        ))}
      </div>
      {data?.days?.length > 1 && (
        <div className="flex items-end gap-1 h-16 rounded-input border border-rule bg-paper px-3 pt-3 pb-2" aria-label="tokens per day">
          {data.days.map((d) => {
            const v = d.input_tokens + d.output_tokens;
            return (
              <span key={d.day} className="flex-1 bg-brass/70 rounded-t-sm min-h-[2px]" style={{ height: `${(v / peak) * 100}%` }}
                title={`${d.day}: ${tokens(v)} tokens, ${d.calls} calls`} />
            );
          })}
        </div>
      )}
      {data ? <UsageCard usage={data} title={days === 1 ? 'last 24 hours' : `last ${days} days`} />
        : <p className="text-sm text-muted flex items-center gap-2"><Loader2 size={13} className="animate-spin" /> loading…</p>}
    </div>
  );
}
