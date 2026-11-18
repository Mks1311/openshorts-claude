import React, { useState, useEffect, useRef } from 'react';
import { Link2, Upload, FileVideo, X, Info, Loader2, ChevronDown, ArrowRight } from 'lucide-react';
import { useServerConfig } from '../lib/serverConfig';

const SUPPORTED_PLATFORMS = [
    'YouTube', 'Vimeo', 'TikTok', 'X / Twitter', 'Twitch', 'Kick',
    'Facebook', 'Instagram', 'Dailymotion', 'Reddit', 'Streamable',
];

// Mirrors the server's MIN_SOURCE_SECONDS: shorter sources are rejected with a
// 400 after the whole file was uploaded, so the browser says it the moment
// the file is picked.
const MIN_SOURCE_SECONDS = 45;

// "1:05:30", "65:30" or "65" (minutes) -> seconds; null when blank or unreadable.
const parseClock = (text) => {
    const parts = String(text || '').trim().split(':');
    if (!parts[0] || parts.length > 3 || parts.some((p) => !/^\d+(\.\d+)?$/.test(p))) return null;
    if (parts.length === 1) return Number(parts[0]) * 60;
    return parts.reduce((total, p) => total * 60 + Number(p), 0);
};

// Duration of a local video file in seconds, or null when the browser cannot
// read it (unsupported codec): then the server stays the judge.
const readVideoDuration = (file) => new Promise((resolve) => {
    try {
        const url = URL.createObjectURL(file);
        const v = document.createElement('video');
        v.preload = 'metadata';
        const done = (d) => { URL.revokeObjectURL(url); resolve(d); };
        v.onloadedmetadata = () => done(Number.isFinite(v.duration) ? v.duration : null);
        v.onerror = () => done(null);
        setTimeout(() => done(null), 8000);
        v.src = url;
    } catch { resolve(null); }
});

const FORMATS = [
    { value: 'vertical', label: '9:16', hint: 'Shorts · Reels · TikTok', w: 15, h: 26 },
    { value: 'square', label: '1:1', hint: 'Feed posts', w: 22, h: 22 },
    { value: 'horizontal', label: '16:9', hint: 'Landscape', w: 30, h: 17 },
];

export default function MediaInput({ onProcess, isProcessing }) {
    const { youtubeUrlEnabled = true } = useServerConfig();
    const [mode, setMode] = useState('file'); // 'file' | 'url'
    const [url, setUrl] = useState('');
    const [file, setFile] = useState(null);
    const [dragging, setDragging] = useState(false);
    const [fileSeconds, setFileSeconds] = useState(null);
    const fileTooShort = fileSeconds != null && fileSeconds < MIN_SOURCE_SECONDS;
    // A stretch of the source (typed start/end); blank = the whole video.
    const [startText, setStartText] = useState('');
    const [endText, setEndText] = useState('');
    const typedStart = parseClock(startText);
    const typedEnd = parseClock(endText);
    const rangeError = (startText || endText)
        ? (typedEnd == null ? 'Enter an end time.'
            : (startText && typedStart == null) ? 'Start time not understood.'
            : typedEnd - (typedStart || 0) < 60 ? 'Pick at least 1 minute.' : null)
        : null;
    const [outputFormat, setOutputFormat] = useState('vertical');
    const [showInfo, setShowInfo] = useState(false);
    // Advanced generation controls: empty means "let Claude decide".
    const [showAdvanced, setShowAdvanced] = useState(false);
    const [targetClips, setTargetClips] = useState('');
    const [clipMinSeconds, setClipMinSeconds] = useState('');
    const [clipMaxSeconds, setClipMaxSeconds] = useState('');
    // Auto-hook: burn the AI hook text into every clip. The choice persists.
    const [autoHook, setAutoHook] = useState(() => {
        try { return localStorage.getItem('os_auto_hook') !== '0'; } catch { return true; }
    });
    const [autoHookStyle, setAutoHookStyle] = useState(() => {
        try { return localStorage.getItem('os_auto_hook_style_v2') || 'pill'; } catch { return 'pill'; }
    });
    // Layout: 'auto' lets Claude pick per video (server default).
    const [layout, setLayout] = useState(() => {
        try { return localStorage.getItem('os_layout') || 'auto'; } catch { return 'auto'; }
    });
    const infoRef = useRef(null);

    useEffect(() => { if (!youtubeUrlEnabled) setMode('file'); }, [youtubeUrlEnabled]);

    // Close the supported-sites popover on any outside click.
    useEffect(() => {
        if (!showInfo) return undefined;
        const onClick = (e) => {
            if (infoRef.current && !infoRef.current.contains(e.target)) setShowInfo(false);
        };
        document.addEventListener('mousedown', onClick);
        return () => document.removeEventListener('mousedown', onClick);
    }, [showInfo]);

    useEffect(() => {
        let cancelled = false;
        setFileSeconds(null);
        if (!file) return undefined;
        readVideoDuration(file).then((d) => { if (!cancelled) setFileSeconds(d); });
        return () => { cancelled = true; };
    }, [file]);

    const ready = mode === 'url' ? !!url : !!file && !fileTooShort;
    const advancedSet = targetClips || clipMinSeconds || clipMaxSeconds || !autoHook || layout !== 'auto';

    const handleSubmit = (e) => {
        e.preventDefault();
        if (!ready || rangeError) return;
        const typedRange = typedEnd != null;
        const advanced = {
            targetClips: targetClips || null,
            clipMinSeconds: clipMinSeconds || null,
            clipMaxSeconds: clipMaxSeconds || null,
            autoHook,
            autoHookStyle,
            layout,
            sourceStart: typedRange ? Math.round(typedStart || 0) : null,
            sourceEnd: typedRange ? Math.round(typedEnd) : null,
        };
        try {
            localStorage.setItem('os_auto_hook', autoHook ? '1' : '0');
            localStorage.setItem('os_auto_hook_style_v2', autoHookStyle);
            localStorage.setItem('os_layout', layout);
        } catch { /* ignore */ }
        // Your own install, your own videos: the server's rights attestation
        // (a hosted-service requirement) is given for you.
        if (mode === 'url') {
            onProcess({ type: 'url', payload: url, acknowledged: true, outputFormat, ...advanced });
        } else {
            onProcess({ type: 'file', payload: file, acknowledged: true, outputFormat, ...advanced });
        }
    };

    const handleDrop = (e) => {
        e.preventDefault();
        setDragging(false);
        if (e.dataTransfer.files && e.dataTransfer.files[0]) {
            setFile(e.dataTransfer.files[0]);
            setMode('file');
        }
    };

    return (
        <form onSubmit={handleSubmit} className="card animate-fade">
            {/* Source */}
            <div className="panel-section p-4 sm:p-6">
                <div className="flex items-center justify-between gap-3 mb-4">
                    <p className="eyebrow">Source</p>
                    {youtubeUrlEnabled && (
                        <div className="flex gap-1.5" role="tablist" aria-label="source">
                            <button type="button" role="tab" aria-selected={mode === 'file'}
                                onClick={() => setMode('file')} className={`chip ${mode === 'file' ? 'chip-on' : ''}`}>
                                <Upload size={13} /> file
                            </button>
                            <button type="button" role="tab" aria-selected={mode === 'url'}
                                onClick={() => setMode('url')} className={`chip ${mode === 'url' ? 'chip-on' : ''}`}>
                                <Link2 size={13} /> link
                            </button>
                        </div>
                    )}
                </div>

                {mode === 'url' ? (
                    <div className="relative" ref={infoRef}>
                        <input
                            type="url"
                            value={url}
                            onChange={(e) => setUrl(e.target.value)}
                            placeholder="https://youtube.com/watch?v=…"
                            className="input-field pr-11 py-3.5"
                            autoFocus
                        />
                        <div className="absolute inset-y-0 right-2 flex items-center">
                            <button
                                type="button"
                                onClick={() => setShowInfo((v) => !v)}
                                aria-label="Supported sites"
                                className="p-1.5 text-muted hover:text-brass transition-colors"
                            >
                                <Info size={16} />
                            </button>
                        </div>
                        {showInfo && (
                            <div className="absolute right-0 top-full mt-2 w-64 z-20 card p-4 text-left animate-fade">
                                <p className="eyebrow mb-2">Paste a link from</p>
                                <div className="flex flex-wrap gap-1.5">
                                    {SUPPORTED_PLATFORMS.map((p) => (
                                        <span key={p} className="text-xs px-2 py-0.5 rounded-full bg-paper3 text-ink2">{p}</span>
                                    ))}
                                </div>
                                <p className="text-xs text-muted mt-2.5 leading-relaxed">
                                    …and most other sites with a public video (yt-dlp).
                                </p>
                            </div>
                        )}
                    </div>
                ) : file ? (
                    <div className={`rounded-card border px-4 py-4 flex items-center gap-3 min-w-0 ${fileTooShort ? 'border-danger/50' : 'border-brass/50 bg-brass/5'}`}>
                        <div className="w-9 h-9 rounded-input bg-paper3 flex items-center justify-center shrink-0">
                            <FileVideo size={17} className="text-brass" />
                        </div>
                        <div className="min-w-0 flex-1">
                            <p className="text-sm text-ink truncate">{file.name}</p>
                            <p className="readout mt-0.5">
                                {(file.size / 1e6).toFixed(0)} MB
                                {fileSeconds != null && ` · ${Math.floor(fileSeconds / 60)}:${String(Math.round(fileSeconds % 60)).padStart(2, '0')}`}
                            </p>
                        </div>
                        <button
                            type="button"
                            onClick={() => setFile(null)}
                            aria-label="remove file"
                            className="p-1.5 text-muted hover:text-ink hover:bg-paper3 rounded-full transition-colors shrink-0"
                        >
                            <X size={16} />
                        </button>
                    </div>
                ) : (
                    <label
                        className={`group block cursor-pointer rounded-card border border-dashed px-6 py-7 sm:py-8 text-center transition-colors ${dragging ? 'border-brass bg-brass/5' : 'border-rule2 hover:border-brass/70 hover:bg-paper3/40'}`}
                        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
                        onDragLeave={() => setDragging(false)}
                        onDrop={handleDrop}
                    >
                        <input
                            type="file"
                            accept="video/*"
                            onChange={(e) => setFile(e.target.files?.[0] || null)}
                            className="hidden"
                        />
                        <span className="mx-auto mb-3 w-10 h-10 rounded-full bg-paper3 flex items-center justify-center transition-transform duration-200 group-hover:-translate-y-0.5">
                            <Upload size={17} className="text-brass" />
                        </span>
                        <span className="block text-ink2 text-sm">Drop a video here, or <span className="text-brass underline underline-offset-4 decoration-brass/40">browse</span></span>
                        <span className="readout mt-2 block">MP4 · MOV · at least {MIN_SOURCE_SECONDS}s long</span>
                    </label>
                )}
                {file && fileTooShort && mode === 'file' && (
                    <p className="text-danger text-xs mt-3 leading-relaxed" role="alert">
                        This video is {Math.round(fileSeconds)}s long: it already is a short. Clips need at least
                        {' '}{MIN_SOURCE_SECONDS}s of footage to cut from.
                    </p>
                )}

                {/* Part of the source: long videos and livestreams */}
                <div className="mt-4 grid grid-cols-[1fr_auto_1fr] items-end gap-2">
                    <div>
                        <label htmlFor="range-start" className="readout mb-1.5 block">clip from</label>
                        <input
                            id="range-start" type="text" inputMode="numeric"
                            value={startText}
                            onChange={(e) => setStartText(e.target.value)}
                            placeholder="0:00"
                            className="input-field py-2.5"
                        />
                    </div>
                    <ArrowRight size={14} className="text-muted mb-3.5" aria-hidden="true" />
                    <div>
                        <label htmlFor="range-end" className="readout mb-1.5 block">to</label>
                        <input
                            id="range-end" type="text" inputMode="numeric"
                            value={endText}
                            onChange={(e) => setEndText(e.target.value)}
                            placeholder="e.g. 1:35:00"
                            className="input-field py-2.5"
                        />
                    </div>
                    <p className={`col-span-3 text-[11px] leading-relaxed ${rangeError ? 'text-danger' : 'text-muted'}`} role={rangeError ? 'alert' : undefined}>
                        {rangeError || 'Long video or livestream? Clip only part of it: h:mm:ss, mm:ss or minutes. Leave blank for the whole video.'}
                    </p>
                </div>
            </div>

            {/* Output */}
            <div className="panel-section p-4 sm:p-6">
                <p className="eyebrow mb-3">Output</p>
                <div className="grid grid-cols-3 gap-2" role="radiogroup" aria-label="output format">
                    {FORMATS.map((f) => {
                        const active = outputFormat === f.value;
                        return (
                            <button
                                key={f.value}
                                type="button"
                                role="radio"
                                aria-checked={active}
                                onClick={() => setOutputFormat(f.value)}
                                className={`py-3 px-2 rounded-input border flex flex-col items-center gap-2 transition-all duration-200 hover:-translate-y-px
                                    ${active ? 'border-brass bg-brass/5 text-ink' : 'border-rule2 text-muted hover:border-rule2 hover:text-ink2'}`}
                            >
                                <span className="h-7 flex items-center">
                                    <span
                                        className="rounded-[3px] border-[1.5px] transition-colors"
                                        style={{
                                            width: `${f.w}px`,
                                            height: `${f.h}px`,
                                            borderColor: active ? 'var(--color-accent)' : 'var(--color-rule-2)',
                                            backgroundColor: active ? 'color-mix(in srgb, var(--color-accent) 22%, transparent)' : 'transparent',
                                        }}
                                    />
                                </span>
                                <span className="block font-mono text-sm leading-none">{f.label}</span>
                                <span className="block text-[11px] sm:text-[10px] leading-tight text-center text-muted">{f.hint}</span>
                            </button>
                        );
                    })}
                </div>

                <button
                    type="button"
                    onClick={() => setShowAdvanced((v) => !v)}
                    aria-expanded={showAdvanced}
                    className="mt-4 flex items-center gap-1.5 text-xs text-muted hover:text-ink2 lowercase transition-colors"
                >
                    <ChevronDown size={14} className={`transition-transform ${showAdvanced ? 'rotate-180' : ''}`} />
                    clip count, length, layout and hooks
                    {advancedSet && <span className="dot dot-ok ml-1" aria-label="changed" />}
                </button>
                {showAdvanced && (
                    <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-3 sm:gap-2 animate-fade">
                        <div>
                            <label className="readout mb-1.5 block" htmlFor="adv-clips">clips to aim for</label>
                            <input id="adv-clips" type="number" min="1" max="15" step="1" value={targetClips}
                                onChange={(e) => setTargetClips(e.target.value)} placeholder="auto" className="input-field py-2.5" />
                        </div>
                        <div>
                            <label className="readout mb-1.5 block" htmlFor="adv-min">min length (s)</label>
                            <input id="adv-min" type="number" min="5" max="175" step="1" value={clipMinSeconds}
                                onChange={(e) => setClipMinSeconds(e.target.value)} placeholder="15" className="input-field py-2.5" />
                        </div>
                        <div>
                            <label className="readout mb-1.5 block" htmlFor="adv-max">max length (s)</label>
                            <input id="adv-max" type="number" min="10" max="180" step="1" value={clipMaxSeconds}
                                onChange={(e) => setClipMaxSeconds(e.target.value)} placeholder="60" className="input-field py-2.5" />
                        </div>
                        <p className="sm:col-span-3 text-[11px] leading-relaxed text-muted">
                            Targets, not guarantees: Claude returns fewer clips when the material doesn&apos;t hold them.
                        </p>
                        <div className="sm:col-span-3 flex flex-wrap items-center justify-between gap-3 pt-3 border-t border-rule">
                            <span className="text-xs text-ink2">vertical layout</span>
                            <select value={layout} onChange={(e) => setLayout(e.target.value)}
                                className="input-field !w-auto text-xs py-1.5" aria-label="vertical layout">
                                <option value="auto">Auto (Claude picks per video)</option>
                                <option value="split">Two speakers stacked</option>
                                <option value="screencast">Screen over presenter</option>
                                <option value="none">Single crop only</option>
                            </select>
                        </div>
                        <div className="sm:col-span-3 flex flex-wrap items-center justify-between gap-3 pt-3 border-t border-rule">
                            <label className="flex items-center gap-2 text-xs text-ink2 cursor-pointer select-none">
                                <input type="checkbox" checked={autoHook} onChange={(e) => setAutoHook(e.target.checked)}
                                    className="w-4 h-4 shrink-0 accent-[var(--color-accent)] cursor-pointer" />
                                hook title on every clip
                            </label>
                            {autoHook && (
                                <select value={autoHookStyle} onChange={(e) => setAutoHookStyle(e.target.value)}
                                    className="input-field !w-auto text-xs py-1.5" aria-label="hook style">
                                    <option value="pill">Pills</option>
                                    <option value="classic">Classic</option>
                                    <option value="dark">Dark</option>
                                    <option value="yellow">Yellow</option>
                                    <option value="red">Red</option>
                                    <option value="outline">Outline</option>
                                    <option value="outline_yellow">Outline+</option>
                                </select>
                            )}
                        </div>
                    </div>
                )}
            </div>

            <div className="panel-section p-4 sm:p-6">
                <button
                    type="submit"
                    disabled={isProcessing || !ready || !!rangeError}
                    className="w-full btn-primary py-3.5"
                >
                    {isProcessing
                        ? <><Loader2 size={16} className="animate-spin" /> starting…</>
                        : <>generate clips <ArrowRight size={16} /></>}
                </button>
            </div>
        </form>
    );
}
