import React, { useEffect, useState } from 'react';
import { Youtube, Loader2, AlertCircle, ExternalLink, Check } from 'lucide-react';
import { apiJson } from '../lib/api';
import { getApiUrl } from '../config';
import Modal from './ui/Modal';
import SegmentedControl from './ui/SegmentedControl';

const PRIVACY_OPTIONS = [
  { value: 'private', label: 'private' },
  { value: 'unlisted', label: 'unlisted' },
  { value: 'public', label: 'public' },
];

const AUDIT_NOTE = 'Until Google audits this app, YouTube keeps every upload private: switch it to public in YouTube Studio.';

const jsonPut = (path, body, method = 'PUT') => apiJson(path, {
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

function useYouTubeStatus(load = true) {
  const [st, setSt] = useState(null);
  useEffect(() => {
    if (load) apiJson('/api/youtube/status').then(setSt).catch(() => setSt(false));
  }, [load]);
  return [st, setSt];
}

// Settings → YouTube: connect the channel, and the optional auto-upload.
export function YouTubeSettings() {
  const [st, setSt] = useYouTubeStatus();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const run = async (fn) => {
    setBusy(true);
    setError('');
    try { setSt(await fn()); } catch (e) { setError(e.detail || 'Could not save.'); } finally { setBusy(false); }
  };

  if (st === false) return <p className="text-sm text-muted">Direct YouTube upload is not available on this server.</p>;
  if (!st) return <p className="text-sm text-muted flex items-center gap-2"><Loader2 size={13} className="animate-spin" /> loading…</p>;
  if (!st.configured) {
    return (
      <p className="text-sm text-muted leading-relaxed">
        Set <code className="font-mono text-ink2">YOUTUBE_CLIENT_ID</code> and <code className="font-mono text-ink2">YOUTUBE_CLIENT_SECRET</code> in
        {' '}<code className="font-mono text-ink2">.env</code> (a Google Cloud OAuth web client) and restart the backend.
      </p>
    );
  }

  const connectUrl = getApiUrl('/api/youtube/connect');
  const auto = st.auto;
  const saveAuto = (patch) => run(() => jsonPut('/api/youtube/auto', { ...auto, ...patch }));

  return (
    <div className="space-y-4">
      {!st.connected ? (
        <a href={connectUrl} className="btn-primary w-full sm:w-auto"><Youtube size={16} /> connect youtube</a>
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-input border border-rule bg-paper px-3 py-2.5">
          <span className="text-sm text-ink2 truncate">
            {st.reconnect_needed
              ? <span className="text-warn">access expired: reconnect</span>
              : <>connected{st.channel?.title ? <> as <span className="text-ink">{st.channel.title}</span></> : ''}</>}
          </span>
          <span className="flex gap-2">
            <a href={connectUrl} className={st.reconnect_needed ? 'btn-primary py-1.5 px-3 text-sm' : 'btn-quiet py-1.5 px-3 text-sm'}>reconnect</a>
            <button type="button" disabled={busy} onClick={() => run(() => jsonPut('/api/youtube/disconnect', {}, 'POST'))}
              className="btn-quiet py-1.5 px-3 text-sm">disconnect</button>
          </span>
        </div>
      )}
      {st.error && <p className="text-xs text-danger">{st.error}</p>}
      <p className="text-xs text-muted leading-relaxed">
        {AUDIT_NOTE} While the Google app is in Testing, its access lasts 7 days: reconnect when this says so.
        Each upload uses about 1,600 of the 10,000 daily API units (about 6 a day).
      </p>

      {st.connected && (
        <div className="rounded-input border border-rule bg-paper p-3 space-y-3">
          <label className="flex items-center justify-between gap-3 cursor-pointer">
            <span className="text-sm text-ink2">upload the best clips of every finished job</span>
            <input type="checkbox" checked={auto.enabled} disabled={busy}
              onChange={(e) => saveAuto({ enabled: e.target.checked })} className="w-4 h-4 accent-brass cursor-pointer" />
          </label>
          {auto.enabled && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 animate-fade">
              <label className="text-xs text-muted">top clips
                <input type="number" min={1} max={20} value={auto.top_n} disabled={busy}
                  onChange={(e) => saveAuto({ top_n: Number(e.target.value) || 1 })} className="input-field py-1.5 mt-1" />
              </label>
              <label className="text-xs text-muted">hours between public posts
                <input type="number" min={0} max={168} value={auto.spacing_hours} disabled={busy}
                  onChange={(e) => saveAuto({ spacing_hours: Number(e.target.value) || 0 })} className="input-field py-1.5 mt-1" />
              </label>
              <div className="sm:col-span-2">
                <SegmentedControl options={PRIVACY_OPTIONS} value={auto.privacy} onChange={(v) => saveAuto({ privacy: v })} />
              </div>
              <p className="sm:col-span-2 text-xs text-muted">When the daily quota runs out, the rest go up after it resets (midnight Pacific).</p>
            </div>
          )}
        </div>
      )}
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  );
}

// A clip's "youtube" action: upload with title, description, privacy and an optional schedule.
export function YouTubeUploadButton({ jobId, index, clip, posted, className }) {
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState(posted || null);
  const [st] = useYouTubeStatus(open);
  const [title, setTitle] = useState(clip.video_title_for_youtube_short || '');
  const [description, setDescription] = useState(clip.video_description_for_tiktok || clip.video_description_for_instagram || '');
  const [privacy, setPrivacy] = useState('private');
  const [when, setWhen] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => { if (posted) setDone(posted); }, [posted]);

  const send = async () => {
    setSending(true);
    setError('');
    try {
      setDone(await jsonPut('/api/youtube/upload', {
        job_id: jobId, clip_index: index, title, description, privacy,
        publish_at: when ? new Date(when).toISOString() : null,
      }, 'POST'));
    } catch (e) {
      setError(e.detail || 'The upload failed.');
    } finally {
      setSending(false);
    }
  };

  const notReady = st && (!st.connected || st.reconnect_needed);

  return (
    <>
      {done ? (
        <a href={done.url} target="_blank" rel="noopener noreferrer" className={className}>
          <ExternalLink size={16} className="text-brass shrink-0" /> on youtube ↗
        </a>
      ) : (
        <button type="button" onClick={() => setOpen(true)} className={className}>
          <Youtube size={16} className="text-muted group-hover:text-brass transition-colors shrink-0" /> youtube
        </button>
      )}
      <Modal isOpen={open} onClose={() => setOpen(false)} eyebrow="YOUTUBE SHORTS" title="upload to youtube" size="md"
        footer={done ? (
          <a href={done.url} target="_blank" rel="noopener noreferrer" className="btn-primary w-full"><ExternalLink size={16} /> open on youtube</a>
        ) : (
          <button type="button" onClick={send} disabled={sending || !st || notReady} className="btn-primary w-full">
            {sending ? <><Loader2 size={16} className="animate-spin" /> uploading…</> : <><Youtube size={16} /> {when ? 'schedule' : 'upload'}</>}
          </button>
        )}>
        {notReady && (
          <div className="mb-4 px-3 py-2 rounded-input text-xs text-warn flex items-start gap-2 border border-rule">
            <AlertCircle size={14} className="mt-0.5 shrink-0" />
            {st.reconnect_needed ? 'YouTube access expired: reconnect it in Settings.' : 'Connect YouTube in Settings first.'}
          </div>
        )}
        {done ? (
          <p className="badge-ok"><Check size={12} className="shrink-0" /> uploaded as {done.privacy}{done.publish_at ? `, publishes ${new Date(done.publish_at).toLocaleString()}` : ''}</p>
        ) : (
          <div className="space-y-4">
            <div>
              <label className="eyebrow block mb-1.5" htmlFor={`yt-title-${index}`}>TITLE</label>
              <input id={`yt-title-${index}`} type="text" maxLength={100} value={title} onChange={(e) => setTitle(e.target.value)} className="input-field" />
            </div>
            <div>
              <label className="eyebrow block mb-1.5" htmlFor={`yt-desc-${index}`}>DESCRIPTION</label>
              <textarea id={`yt-desc-${index}`} rows={4} value={description} onChange={(e) => setDescription(e.target.value)} className="input-field resize-none" />
              <p className="text-[11px] text-muted mt-1">#Shorts is added when neither field has it.</p>
            </div>
            <div>
              <label className="eyebrow block mb-2">PRIVACY</label>
              <SegmentedControl options={PRIVACY_OPTIONS} value={privacy} onChange={setPrivacy} />
            </div>
            <div>
              <label className="eyebrow block mb-1.5" htmlFor={`yt-when-${index}`}>PUBLISH AT (OPTIONAL)</label>
              <input id={`yt-when-${index}`} type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} className="input-field [color-scheme:dark]" />
              {when && <p className="text-[11px] text-muted mt-1">Uploaded private, YouTube makes it public at that time.</p>}
            </div>
            <p className="text-[11px] text-muted leading-relaxed">{AUDIT_NOTE}</p>
            {error && <p className="badge-danger"><AlertCircle size={12} className="shrink-0" /> {error}</p>}
          </div>
        )}
      </Modal>
    </>
  );
}
