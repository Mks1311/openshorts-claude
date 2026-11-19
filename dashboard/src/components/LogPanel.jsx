import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Terminal, ChevronDown, Search, Copy, Download, Check, Bot, AlertTriangle, CircleDot } from 'lucide-react';

// What a line is, read from main.py's own prints.
const CLAUDE = /^🤖 \[claude\] (\S+) · (\S+) · ([\d,]+) in \/ ([\d,]+) out tokens · ([\d.]+)s( · failed)?/;
const STEP = /Downloading|Destination:|Transcribing video|Detected language|Analyzing with the claude CLI|analyzing by vision|Found \d+ (?:viral )?clips|Clip \d+ ready|Processing Clip \d+|Job started by worker|queued\.|Stopped|completed|Job recovered|Resuming/i;
const NOISE = /^\s*\[\d+\.\d+s -> \d+\.\d+s\]|^frame=|^\s*\d+%\||\[ctranslate2\]/;

function classify(line) {
  const text = String(line);
  if (CLAUDE.test(text)) return CLAUDE.exec(text)[6] ? 'claude_failed' : 'claude';
  if (/⚠️|warn|retrying/i.test(text)) return 'warn';
  if (/error|traceback|exception|failed|❌/i.test(text)) return 'error';
  if (STEP.test(text)) return 'step';
  if (NOISE.test(text)) return 'noise';
  return 'info';
}

const FILTERS = [
  { id: 'steps', label: 'key steps', keep: (k) => ['step', 'error', 'claude', 'claude_failed'].includes(k) },
  { id: 'claude', label: 'claude calls', keep: (k) => k === 'claude' || k === 'claude_failed' },
  { id: 'errors', label: 'problems', keep: (k) => ['error', 'warn', 'claude_failed'].includes(k) },
  { id: 'all', label: 'everything', keep: () => true },
];

const TONE = {
  error: 'text-danger',
  warn: 'text-warn',
  step: 'text-ink',
  claude: 'text-ink2',
  claude_failed: 'text-danger',
  info: 'text-muted',
  noise: 'text-muted/60',
};

function ClaudeLine({ text }) {
  const [, task, model, tin, tout, secs, failed] = CLAUDE.exec(text);
  return (
    <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <Bot size={12} className="text-brass shrink-0" />
      <span className="text-ink">{task.replace(/_/g, ' ')}</span>
      <span className="px-1.5 rounded bg-paper3 text-[10.5px]">{model}</span>
      <span className="text-muted">{tin} in · {tout} out · {secs}s</span>
      {failed && <span className="text-danger">failed</span>}
    </span>
  );
}

// The job's log, read as a sequence of steps instead of a raw terminal dump.
export default function LogPanel({ logs, times, running, open, onToggle, className = '' }) {
  const [filter, setFilter] = useState('steps');
  const [query, setQuery] = useState('');
  const [copied, setCopied] = useState(false);
  const box = useRef(null);
  const stick = useRef(true);

  const rows = useMemo(() => {
    const keep = FILTERS.find((f) => f.id === filter).keep;
    const q = query.trim().toLowerCase();
    return (logs || []).map((line, i) => ({ line: String(line), i, kind: classify(line) }))
      .filter((r) => keep(r.kind) && (!q || r.line.toLowerCase().includes(q)));
  }, [logs, filter, query]);
  const problems = useMemo(() => (logs || []).filter((l) => ['error', 'claude_failed'].includes(classify(l))).length, [logs]);

  // Follow new lines unless the reader scrolled up to look at something.
  useEffect(() => {
    if (open && stick.current && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [rows, open]);

  const plain = () => (logs || []).map((l, i) => {
    const t = times?.[i] ? new Date(times[i] * 1000).toLocaleTimeString() : '';
    return `${t} ${l}`.trim();
  }).join('\n');
  const copy = async () => {
    try { await navigator.clipboard.writeText(plain()); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* no clipboard */ }
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([plain()], { type: 'text/plain' }));
    const a = document.createElement('a');
    a.href = url; a.download = 'job-log.txt'; a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className={`bg-paper rounded-input border border-rule overflow-hidden flex flex-col ${className}`}>
      <div className="px-3 sm:px-4 py-2 border-b border-rule bg-paper2 shrink-0 flex items-center gap-2">
        <button type="button" onClick={onToggle} aria-expanded={open} className="flex items-center gap-2 min-w-0 flex-1 text-left">
          <span className="readout flex items-center gap-2"><Terminal size={12} /> log</span>
          <span className="readout normal-case tracking-normal">{(logs || []).length} lines</span>
          {problems > 0 && <span className="badge-danger"><AlertTriangle size={10} /> {problems}</span>}
          {running && <CircleDot size={11} className="text-brass animate-pulse" />}
        </button>
        {open && (
          <span className="flex items-center gap-1">
            <button type="button" onClick={copy} className="p-1.5 rounded text-muted hover:text-ink" aria-label="copy log" title="copy log">
              {copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
            </button>
            <button type="button" onClick={download} className="p-1.5 rounded text-muted hover:text-ink" aria-label="download log" title="download log">
              <Download size={13} />
            </button>
          </span>
        )}
        <button type="button" onClick={onToggle} className="p-1 text-muted" aria-label={open ? 'hide log' : 'show log'}>
          <ChevronDown size={16} className={open ? '' : 'rotate-180'} />
        </button>
      </div>

      {open && (
        <>
          <div className="px-3 sm:px-4 py-2 border-b border-rule flex flex-wrap items-center gap-2 shrink-0">
            <div className="flex gap-1" role="tablist" aria-label="log filter">
              {FILTERS.map((f) => (
                <button key={f.id} type="button" role="tab" aria-selected={filter === f.id} onClick={() => setFilter(f.id)}
                  className={`px-2 py-0.5 rounded-full text-[11px] border transition-colors ${filter === f.id ? 'border-brass text-ink bg-brass/10' : 'border-rule text-muted hover:text-ink2'}`}>
                  {f.label}
                </button>
              ))}
            </div>
            <label className="ml-auto flex items-center gap-1.5 text-muted min-w-[8rem] flex-1 sm:flex-none">
              <Search size={12} />
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="search"
                className="bg-transparent text-xs text-ink2 outline-none w-full sm:w-32" aria-label="search the log" />
            </label>
          </div>
          <div ref={box} onScroll={(e) => {
            const el = e.currentTarget;
            stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
          }}
            className="flex-1 min-h-0 p-3 sm:p-4 overflow-y-auto font-mono text-[11px] sm:text-xs space-y-1 custom-scrollbar break-words">
            {rows.length === 0 && <p className="text-muted">{query ? 'No line matches.' : 'Nothing here yet.'}</p>}
            {rows.map(({ line, i, kind }) => (
              <div key={i} className={`flex gap-2 ${TONE[kind]} ${kind === 'step' ? 'font-medium' : ''}`}>
                <span className="opacity-50 shrink-0 hidden sm:inline tabular-nums text-muted">
                  {times?.[i] ? new Date(times[i] * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : ''}
                </span>
                <span className="min-w-0 break-words flex-1">
                  {kind.startsWith('claude') ? <ClaudeLine text={line} /> : line}
                </span>
              </div>
            ))}
            {running && filter !== 'errors' && <div className="animate-pulse text-brass">_</div>}
          </div>
        </>
      )}
    </div>
  );
}
