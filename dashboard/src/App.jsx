import React, { useState, useEffect, useRef, useMemo } from 'react';
import {
  Youtube, Instagram, Share2, ChevronDown, Check, LayoutDashboard, Settings, Plus, X, Terminal,
  Image, RotateCcw, Calendar, Loader2, Download, Clock, ScanFace, Captions, AudioLines, Scissors,
  Sparkles, History, ListOrdered, Trash2,
} from 'lucide-react';
import MediaInput from './components/MediaInput';
import JobList from './components/JobList';
import QueuePage from './components/QueuePage';
import LogPanel from './components/LogPanel';
import UsageCard from './components/UsageCard';
import { ModelPicker, UsageSummary } from './components/ModelSettings';
import { useJobs, isActive, deleteJob } from './lib/jobs';
import { YouTubeSettings } from './components/YouTubePublish';
import ResultCard from './components/ResultCard';
import ProcessingAnimation from './components/ProcessingAnimation';
import { clipProgress, jobStage } from './lib/clipProgress';
import ThumbnailStudio from './components/ThumbnailStudio';
import ScheduleWeekModal from './components/ScheduleWeekModal';
import ClipEditor from './components/ClipEditor';
import ReframeEditor from './components/ReframeEditor';
import Modal from './components/ui/Modal';
import { apiFetch, apiJson } from './lib/api';
import { useServerConfig } from './lib/serverConfig';
import { SOCIAL_PLATFORMS } from './lib/socialPlatforms';
import { JobPollError, pollFailureAction } from './lib/jobPoll';

// Keys are kept in this browser only, lightly obfuscated (XOR + base64) so
// they do not sit in localStorage as plain text. Not encryption against
// anyone with access to the machine.
const SECRET_KEY = import.meta.env.VITE_ENCRYPTION_KEY || "OpenShorts-Static-Salt-Change-Me";
const ENCRYPTION_PREFIX = "ENC:";

const encrypt = (text) => {
  if (!text) return '';
  try {
    const xor = text.split('').map((c, i) =>
      String.fromCharCode(c.charCodeAt(0) ^ SECRET_KEY.charCodeAt(i % SECRET_KEY.length))
    ).join('');
    return ENCRYPTION_PREFIX + btoa(xor);
  } catch {
    return text;
  }
};

const decrypt = (text) => {
  if (!text) return '';
  if (text.startsWith(ENCRYPTION_PREFIX)) {
    try {
      const xor = atob(text.slice(ENCRYPTION_PREFIX.length));
      return xor.split('').map((c, i) =>
        String.fromCharCode(c.charCodeAt(0) ^ SECRET_KEY.charCodeAt(i % SECRET_KEY.length))
      ).join('');
    } catch {
      return '';
    }
  }
  return text;
};

const TikTokIcon = ({ size = 16, className = "" }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden="true">
    <path d="M19.589 6.686a4.793 4.793 0 0 1-3.77-4.245V2h-3.445v13.672a2.896 2.896 0 0 1-5.201 1.743l-.002-.001.002.001a2.895 2.895 0 0 1 3.183-4.51v-3.5a6.329 6.329 0 0 0-5.394 10.692 6.33 6.33 0 0 0 10.857-4.424V8.687a8.182 8.182 0 0 0 4.773 1.526V6.79a4.831 4.831 0 0 1-1.003-.104z" />
  </svg>
);

// TikTok, Instagram and YouTube have their own icons; the rest come from the shared list.
const EXTRA_NETWORKS = SOCIAL_PLATFORMS.slice(3);

const NetworkIcons = ({ connected = [], size = 12 }) => (
  <span className="flex items-center gap-1.5">
    <span className={connected.includes('tiktok') ? 'text-ink' : 'text-muted opacity-40'}><TikTokIcon size={size} /></span>
    <span className={connected.includes('instagram') ? 'text-ink' : 'text-muted opacity-40'}><Instagram size={size} /></span>
    <span className={connected.includes('youtube') ? 'text-ink' : 'text-muted opacity-40'}><Youtube size={size} /></span>
    {EXTRA_NETWORKS.map((p) => (
      <span key={p.value} className={connected.includes(p.value) ? 'text-ink' : 'text-muted opacity-40'}><p.Icon size={size} /></span>
    ))}
  </span>
);

// The Upload-Post profile clips are posted as.
const ProfileSelector = ({ profiles, selectedUserId, onSelect }) => {
  const [isOpen, setIsOpen] = useState(false);
  if (!profiles || profiles.length === 0) return null;
  const selected = profiles.find(p => p.username === selectedUserId) || profiles[0];

  return (
    <div className="relative z-50">
      <button
        onClick={() => setIsOpen(!isOpen)}
        aria-label="posting profile"
        aria-expanded={isOpen}
        className="flex items-center gap-2 bg-paper2 border border-rule2 rounded-full pl-1.5 pr-2.5 py-1.5 text-sm text-ink2 hover:border-brass/60 transition-colors"
      >
        <span className="w-6 h-6 rounded-full bg-paper3 border border-rule flex items-center justify-center font-mono text-micro text-brass shrink-0">
          {selected?.username?.substring(0, 1).toUpperCase() || "U"}
        </span>
        <span className="hidden sm:block text-ink truncate max-w-[110px]">{selected?.username}</span>
        <ChevronDown size={14} className={`text-muted transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {isOpen && (
        <div className="absolute top-full mt-2 right-0 w-64 card overflow-hidden animate-fade">
          <p className="eyebrow px-4 pt-3 pb-2">post as</p>
          <div className="max-h-60 overflow-y-auto custom-scrollbar">
            {profiles.map((profile) => (
              <button
                key={profile.username}
                onClick={() => { onSelect(profile.username); setIsOpen(false); }}
                className="w-full flex items-center justify-between px-4 py-3 hover:bg-paper3 transition-colors text-left border-t border-rule"
              >
                <span className="min-w-0">
                  <span className="block text-sm text-ink2 truncate">{profile.username}</span>
                  <span className="block mt-1"><NetworkIcons connected={profile.connected || []} size={11} /></span>
                </span>
                {selectedUserId === profile.username && <Check size={14} className="text-brass shrink-0" />}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

const SESSION_KEY = 'openshorts_session';
// Matches the server's JOB_RETENTION_SECONDS default: a session older than
// this points at a job the server has already cleared.
const SESSION_MAX_AGE = 86400000;

// The server's own explanation for a rejected or failed job, readable: FastAPI
// answers {"detail": "..."} or {"detail": {"message": ...}}.
const readableError = (raw) => {
  const text = String(raw || '').trim();
  try {
    const body = JSON.parse(text);
    const d = body?.detail ?? body;
    if (typeof d === 'string' && d) return d.slice(0, 300);
    if (d && typeof d.message === 'string') return d.message.slice(0, 300);
  } catch { /* not JSON */ }
  return text.replace(/^Error:\s*/, '').slice(0, 300) || 'Something went wrong.';
};

const formatRetention = (seconds) => {
  if (seconds >= 86400) return `${Math.round(seconds / 86400)} day${seconds >= 172800 ? 's' : ''}`;
  if (seconds >= 3600) return `${Math.round(seconds / 3600)} hour${seconds >= 7200 ? 's' : ''}`;
  return `${Math.max(1, Math.round(seconds / 60))} min`;
};

const pollJob = async (jobId) => {
  const res = await apiFetch(`/api/status/${jobId}`);
  if (!res.ok) throw new JobPollError(res.status);
  return res.json();
};

// What one run does, in order. Shown next to the form and as the rail of the
// pipeline: each step is a real stage of main.py.
const PIPELINE = [
  { icon: AudioLines, title: 'Transcribe', text: 'Whisper writes every word with its timestamp.' },
  { icon: Sparkles, title: 'Pick moments', text: 'Claude reads the transcript and frames and ranks the 15–60 s stretches that stand alone.' },
  { icon: ScanFace, title: 'Reframe', text: 'Faces are tracked into 9:16, with split or screen layouts when the video needs them.' },
  { icon: Captions, title: 'Caption & hook', text: 'Word-timed captions and a hook title are burned in, ready to edit.' },
];

// Module level, not inside App: a component defined in render remounts on
// every keystroke and the field loses focus.
const KeyField = ({ id, label, value, onChange, placeholder, onSave, saveLabel, saved }) => (
  <div>
    <label htmlFor={id} className="readout mb-1.5 block">{label}</label>
    <div className="flex flex-col sm:flex-row gap-2">
      <input id={id} type="password" value={value} onChange={(e) => onChange(e.target.value)}
        className="input-field py-2.5" placeholder={placeholder} autoComplete="off" />
      <button onClick={onSave} disabled={!value} className="btn-quiet py-2 px-5 text-sm shrink-0">
        {saved ? <><Check size={14} className="text-ok" /> saved</> : saveLabel}
      </button>
    </div>
  </div>
);

const SettingsSection = ({ eyebrow, title, badge, children }) => (
  <section className="panel-section p-5 sm:p-7">
    <div className="flex flex-wrap items-center justify-between gap-2 mb-1.5">
      <p className="eyebrow">{eyebrow}</p>
      {badge}
    </div>
    <h2 className="font-display lowercase text-xl text-ink mb-2">{title}</h2>
    {children}
  </section>
);

const STATUS_LABEL = { processing: 'working', complete: 'done', error: 'failed' };

function App() {
  const server = useServerConfig();
  const model = server.localLlm?.model || 'sonnet';
  const jobRetentionSeconds = server.jobRetentionSeconds || 0;

  // Where the running job stands in line while it waits, or null.
  const [queueInfo, setQueueInfo] = useState(null);
  // The stretch being clipped ({start_seconds, end_seconds, ...}) when only
  // part of the source was asked for.
  const [partialJob, setPartialJob] = useState(null);
  // Why the last job could not start or failed, in plain words (or '').
  const [jobError, setJobError] = useState('');

  const [uploadPostKey, setUploadPostKey] = useState(() => decrypt(localStorage.getItem('uploadPostKey_v3') || ''));
  const [elevenLabsKey, setElevenLabsKey] = useState(() => decrypt(localStorage.getItem('elevenLabsKey_v1') || ''));
  const [uploadUserId, setUploadUserId] = useState(() => localStorage.getItem('uploadUserId') || '');
  const [userProfiles, setUserProfiles] = useState([]); // [{username, connected: []}]
  const [profilesState, setProfilesState] = useState('idle'); // idle | loading | ok | none | error

  const [jobId, setJobId] = useState(null);
  // Every job on the server; while one is queued or running, a new
  // submission joins the queue.
  const { jobs: allJobs, refresh: refreshJobs } = useJobs();
  const activeJobs = (allJobs || []).filter(isActive);
  const queueBusy = activeJobs.length > 0;
  const [queuedNotice, setQueuedNotice] = useState(false);
  const [status, setStatus] = useState('idle'); // idle, processing, complete, error
  const [stopping, setStopping] = useState(false);
  useEffect(() => { if (status !== 'processing') setStopping(false); }, [status]);
  // The server ends the job within a couple of seconds and the poll then
  // reports it failed, with "Stopped" in the log.
  const stopJob = async () => {
    if (!jobId || !window.confirm('Stop this job? Clips it has not finished are lost.')) return;
    setStopping(true);
    try {
      await apiJson(`/api/jobs/${jobId}/stop`, { method: 'POST' });
    } catch (e) {
      setStopping(false);
      setLogs(l => [...l, `Could not stop the job: ${e.detail || e.message}`]);
    }
  };
  const [results, setResults] = useState(null);
  // Best clips first. The ORIGINAL array position travels with each clip and
  // is what gets passed down as `index`: it is the clip's identity everywhere
  // else (clip_index on the edit endpoints, the clip-N.mp4 download name).
  const rankedClips = useMemo(() => {
    const clips = results?.clips;
    if (!Array.isArray(clips)) return [];
    return clips
      .map((clip, index) => ({ clip, index }))
      .sort((a, b) => {
        const sa = Number.isFinite(a.clip?.predicted_score) ? a.clip.predicted_score : -1;
        const sb = Number.isFinite(b.clip?.predicted_score) ? b.clip.predicted_score : -1;
        return sb - sa || a.index - b.index;
      });
  }, [results]);
  // Bulk subtitles: one style applied to every clip of the job.
  const [bulkSub, setBulkSub] = useState({ running: false, current: 0, total: 0, errors: 0 });
  const [downloadingAll, setDownloadingAll] = useState(false);
  // Pre-flight quality gate: { info: {max_height, min_height, cookies_invalid}, data }
  const [qualityGate, setQualityGate] = useState(null);
  const [logs, setLogs] = useState([]);
  const [logTimes, setLogTimes] = useState([]);
  const [jobUsage, setJobUsage] = useState(null);
  // Clips already on YouTube: {clip index: {url, privacy, ...}} (/api/status).
  const [youtubePosted, setYoutubePosted] = useState({});
  const progress = useMemo(() => clipProgress(logs), [logs]);
  const stage = useMemo(() => jobStage(logs), [logs]);
  const clipsMaking = status === 'processing' && progress.total > 0;
  const [logsVisible, setLogsVisible] = useState(() => {
    try { return window.innerWidth >= 768; } catch { return true; }
  });
  const [processingMedia, setProcessingMedia] = useState(null);
  // Back from Google's consent screen (/api/youtube/callback): open Settings.
  const [activeTab, setActiveTab] = useState(() => {
    if (!new URLSearchParams(window.location.search).has('youtube')) return 'dashboard';
    window.history.replaceState(null, '', window.location.pathname);
    return 'settings';
  });

  const [sessionRecovered, setSessionRecovered] = useState(false);
  const [jobGone, setJobGone] = useState(false);
  const [showScheduleWeek, setShowScheduleWeek] = useState(false);
  const [editingClip, setEditingClip] = useState(null);
  const [reframingClip, setReframingClip] = useState(null);
  const [keySaved, setKeySaved] = useState('');

  // The source preview follows the clip being played.
  const [syncedTime, setSyncedTime] = useState(0);
  const [isSyncedPlaying, setIsSyncedPlaying] = useState(false);
  const [syncTrigger, setSyncTrigger] = useState(0);

  const handleClipPlay = (startTime) => {
    // Clip times are relative to the processed stretch; the preview plays the
    // whole source, so a picked range shifts them by its start.
    setSyncedTime(startTime + (Number(partialJob?.start_seconds) || 0));
    setIsSyncedPlaying(true);
    setSyncTrigger(prev => prev + 1);
  };
  const handleClipPause = () => setIsSyncedPlaying(false);

  // A recut replaced the clip's server file with a fresh render: update the
  // results and let the ResultCard remount from the new file.
  const handleClipRerendered = (index, data) => {
    setResults((prev) => {
      if (!prev?.clips?.[index]) return prev;
      const clips = prev.clips.slice();
      clips[index] = {
        ...clips[index],
        video_url: data.new_video_url,
        start: data.start,
        end: data.end,
        recipe: data.recipe,
        mode: data.mode ?? clips[index].mode,
      };
      return { ...prev, clips };
    });
  };

  // Apply one subtitle style to every clip of the job, one after another.
  const handleBulkSubtitles = async (options) => {
    const clips = results?.clips || [];
    const total = clips.length;
    if (!total) return;
    setBulkSub({ running: true, current: 0, total, errors: 0 });
    let errors = 0;
    for (let i = 0; i < total; i++) {
      setBulkSub({ running: true, current: i + 1, total, errors });
      try {
        const res = await apiFetch('/api/subtitle', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            job_id: jobId,
            clip_index: i,
            position: options.position,
            font_size: options.fontSize,
            font_name: options.fontName,
            font_color: options.fontColor,
            border_color: options.borderColor,
            border_width: options.borderWidth,
            bg_color: options.bgColor,
            bg_opacity: options.bgOpacity,
            style: options.style || 'pill',
            highlight_color: options.highlightColor || '#FFD700',
            effect: options.effect || 'none',
            base_opacity: options.baseOpacity ?? 1.0,
            uppercase: options.uppercase || false,
            reveal: options.reveal || false,
            shadow: options.shadow || 0,
            max_chars: options.maxChars ?? null,
            max_duration: options.maxDuration ?? null,
            // Chain from the clip's current server file (its video_url basename).
            input_filename: (clips[i].video_url || '').split('/').pop(),
          }),
        });
        if (!res.ok) errors++;
      } catch {
        errors++;
      }
    }
    setBulkSub({ running: false, current: total, total, errors });
    try {
      const data = await pollJob(jobId);
      if (data.result) setResults(data.result);
    } catch { /* keep current results */ }
  };

  const handleDownloadAll = async () => {
    if (!jobId) return;
    setDownloadingAll(true);
    try {
      const res = await apiFetch(`/api/jobs/${jobId}/download-all`);
      if (!res.ok) throw new Error(await res.text());
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `shorts_maxxing_clips_${(jobId || '').slice(0, 8)}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      alert(`Download failed: ${e.message}`);
    } finally {
      setDownloadingAll(false);
    }
  };

  // Session recovery: a reload (or a closed tab) comes back to the same job.
  useEffect(() => {
    try {
      const saved = localStorage.getItem(SESSION_KEY);
      if (!saved) return;
      const session = JSON.parse(saved);
      if (Date.now() - session.timestamp > SESSION_MAX_AGE) {
        localStorage.removeItem(SESSION_KEY);
        return;
      }
      if (session.jobId && session.status && session.status !== 'idle') {
        setJobId(session.jobId);
        setResults(session.results || null);
        setProcessingMedia(session.processingMedia || { type: 'server', payload: `/api/source/${session.jobId}` });
        if (session.partialJob) setPartialJob(session.partialJob);
        if (session.activeTab) setActiveTab(session.activeTab);
        setStatus(session.status === 'processing' ? 'processing' : session.status);
        setSessionRecovered(true);
        setTimeout(() => setSessionRecovered(false), 5000);
      }
    } catch {
      localStorage.removeItem(SESSION_KEY);
    }
  }, []);

  useEffect(() => {
    if (status === 'idle') {
      localStorage.removeItem(SESSION_KEY);
      return;
    }
    try {
      // A link serialises as-is; an uploaded file is a blob that cannot, so
      // the recovered preview plays the copy the backend serves instead.
      let persistMedia = null;
      if (processingMedia?.type === 'url') persistMedia = processingMedia;
      else if (processingMedia && jobId) persistMedia = { type: 'server', payload: `/api/source/${jobId}` };
      localStorage.setItem(SESSION_KEY, JSON.stringify({
        jobId, status, results, processingMedia: persistMedia, activeTab, partialJob, timestamp: Date.now(),
      }));
    } catch { /* localStorage full: nothing to recover then */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, status, results, activeTab, partialJob]);

  useEffect(() => {
    if (uploadPostKey) localStorage.setItem('uploadPostKey_v3', encrypt(uploadPostKey));
    if (uploadUserId) localStorage.setItem('uploadUserId', uploadUserId);
  }, [uploadPostKey, uploadUserId]);

  useEffect(() => {
    if (elevenLabsKey) localStorage.setItem('elevenLabsKey_v1', encrypt(elevenLabsKey));
  }, [elevenLabsKey]);

  const fetchUserProfiles = async ({ silent = false } = {}) => {
    if (!uploadPostKey) return;
    setProfilesState('loading');
    try {
      const res = await apiFetch('/api/social/user', { headers: { 'X-Upload-Post-Key': uploadPostKey } });
      if (!res.ok) throw new Error('Failed to fetch');
      const data = await res.json();
      if (data.profiles && data.profiles.length > 0) {
        setUserProfiles(data.profiles);
        setProfilesState('ok');
        if (!uploadUserId) setUploadUserId(data.profiles[0].username);
      } else {
        setProfilesState(silent ? 'idle' : 'none');
      }
    } catch {
      setProfilesState(silent ? 'idle' : 'error');
    }
  };

  useEffect(() => {
    if (uploadPostKey && userProfiles.length === 0) fetchUserProfiles({ silent: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uploadPostKey]);

  const handleResetRef = useRef(null);

  useEffect(() => {
    let interval;
    let failures = 0;
    if (status === 'processing' && jobId) {
      const tick = async () => {
        try {
          const data = await pollJob(jobId);
          failures = 0;
          if (data.result) setResults(data.result);
          if (data.partial) setPartialJob(data.partial);
          setJobUsage(data.usage || null);
          setYoutubePosted(data.youtube || {});
          setQueueInfo(data.status === 'queued' && data.queue ? data.queue : null);

          if (data.status === 'completed') {
            setQueueInfo(null);
            if (data.logs) { setLogs(data.logs); setLogTimes(data.log_times || []); }
            setStatus('complete');
            clearInterval(interval);
          } else if (data.status === 'failed') {
            setStatus('error');
            const errorMsg = data.error || (data.logs && data.logs.length > 0 ? data.logs[data.logs.length - 1] : "Process failed");
            setJobError(readableError(errorMsg));
            setLogs(prev => [...prev, "Error: " + errorMsg]);
            clearInterval(interval);
          } else if (data.logs) {
            setLogs(data.logs);
            setLogTimes(data.log_times || []);
          }
        } catch (e) {
          failures += 1;
          if (pollFailureAction(e, failures) === 'gone') {
            clearInterval(interval);
            handleResetRef.current();
            setSessionRecovered(false);
            setJobGone(true);
          }
        }
      };
      interval = setInterval(tick, 2000);
      tick(); // an opened job shows where it stands now, not in 2 s
    }
    return () => clearInterval(interval);
  }, [status, jobId]);

  const handleProcess = async (data, forceLowQuality = false) => {
    const queueing = queueBusy;
    setStatus('processing');
    setJobError('');
    setLogs(["Starting process..."]);
    setLogTimes([Date.now() / 1000]);
    setResults(null);
    // Studio handovers have no local media object; the preview switches to the
    // backend-served source once the job id is known.
    setProcessingMedia(data.type === 'thumbnail_session' ? null : data);
    setQualityGate(null);
    setPartialJob(null);

    try {
      let body;
      const headers = {};
      // Only what the user set travels; blank means the server default.
      const advanced = {
        target_clips: data.targetClips || null,
        clip_min_seconds: data.clipMinSeconds || null,
        clip_max_seconds: data.clipMaxSeconds || null,
        auto_hook: data.autoHook ? '1' : '0',
        auto_hook_style: data.autoHook ? (data.autoHookStyle || 'pill') : null,
        layouts: data.layout && data.layout !== 'auto' ? data.layout : null,
        source_start: data.sourceStart ?? null,
        source_end: data.sourceEnd ?? null,
      };

      if (data.type === 'url') {
        headers['Content-Type'] = 'application/json';
        body = JSON.stringify({
          url: data.payload,
          acknowledged: !!data.acknowledged,
          output_format: data.outputFormat || 'auto',
          force_low_quality: forceLowQuality,
          ...Object.fromEntries(Object.entries(advanced).filter(([, v]) => v != null)),
        });
      } else if (data.type === 'thumbnail_session') {
        // Handover from YouTube Studio: the video and transcript already live
        // server-side, keyed by the Studio session.
        headers['Content-Type'] = 'application/json';
        body = JSON.stringify({
          thumbnail_session_id: data.payload,
          acknowledged: !!data.acknowledged,
          output_format: data.outputFormat || 'auto',
          ...Object.fromEntries(Object.entries(advanced).filter(([, v]) => v != null)),
        });
      } else {
        const formData = new FormData();
        formData.append('file', data.payload);
        formData.append('acknowledged', data.acknowledged ? 'true' : 'false');
        formData.append('output_format', data.outputFormat || 'auto');
        for (const [k, v] of Object.entries(advanced)) {
          if (v != null) formData.append(k, v);
        }
        body = formData;
      }

      const res = await apiFetch('/api/process', { method: 'POST', headers, body });
      if (!res.ok) throw new Error(await res.text());
      const resData = await res.json();

      // The source is below the recommended resolution: ask before spending
      // a run on it. On confirm the request goes again with force_low_quality.
      if (resData.needs_confirmation) {
        setStatus('idle');
        setQualityGate({ info: resData.quality_check, data });
        return;
      }

      // Behind another job: back to the form, where the list shows it in line.
      if (queueing) {
        handleReset();
        refreshJobs();
        setQueuedNotice(true);
        setTimeout(() => setQueuedNotice(false), 8000);
        return;
      }
      setJobId(resData.job_id);
      setPartialJob(resData.partial || null);
      if (data.type === 'thumbnail_session') {
        setProcessingMedia({ type: 'server', payload: `/api/source/${resData.job_id}` });
      }
    } catch (e) {
      const reason = readableError(e.message);
      setJobError(reason);
      setStatus('error');
      setLogs(l => [...l, `Error starting job: ${reason}`]);
    }
  };

  const handleReset = () => {
    setJobGone(false);
    setStatus('idle');
    setJobId(null);
    setResults(null);
    setLogs([]);
    setLogTimes([]);
    setProcessingMedia(null);
    setPartialJob(null);
    setJobUsage(null);
    setYoutubePosted({});
    setQueueInfo(null);
    setJobError('');
    localStorage.removeItem(SESSION_KEY);
  };
  useEffect(() => { handleResetRef.current = handleReset; });

  // Open a job from the list: the poll above fills in its state.
  const openJob = (job) => {
    handleReset();
    setActiveTab('dashboard');
    setJobId(job.job_id);
    setProcessingMedia({ type: 'server', payload: `/api/source/${job.job_id}` });
    setLogs(['Loading job...']);
    setStatus('processing');
  };

  const saveKey = (which) => {
    setKeySaved(which);
    setTimeout(() => setKeySaved(''), 1800);
  };

  // --- Shell ---------------------------------------------------------------

  const navItems = [
    { id: 'dashboard', icon: LayoutDashboard, label: 'Clips', hint: 'long video → shorts' },
    ...(allJobs ? [{
      id: 'queue', icon: ListOrdered, label: 'Queue',
      hint: queueBusy ? `${activeJobs.filter((j) => j.status === 'processing').length} running · ${activeJobs.filter((j) => j.status === 'queued').length} waiting` : 'nothing running',
      badge: activeJobs.length || null,
    }] : []),
    { id: 'thumbnails', icon: Image, label: 'YouTube Studio', hint: 'titles · thumbnails' },
    { id: 'settings', icon: Settings, label: 'Settings', hint: 'posting · dubbing · agents' },
  ];
  const activeNav = navItems.find((n) => n.id === activeTab);
  const selectedProfile = userProfiles.find((p) => p.username === uploadUserId) || userProfiles[0];

  const ClaudeStatus = ({ compact = false }) => (
    <div className={`flex items-center gap-2.5 ${compact ? '' : 'rounded-input border border-rule bg-paper px-3 py-2.5'}`}
      title={server.online === false ? 'The backend is not answering' : `Every AI step runs on Claude (${model}) through the claude CLI`}>
      <span className={`dot ${server.online === false ? 'dot-off' : server.online ? 'dot-ok' : 'dot-wait'}`} />
      <span className="min-w-0 leading-tight">
        <span className="block text-xs text-ink2 truncate">
          {server.online === false ? 'backend offline' : <>claude · <span className="font-mono">{model}</span></>}
        </span>
        {!compact && <span className="block readout normal-case tracking-normal mt-0.5">runs on this machine</span>}
      </span>
    </div>
  );

  const Sidebar = () => (
    <aside className="hidden md:flex w-[76px] lg:w-60 bg-paper2 border-r border-rule flex-col h-full shrink-0">
      <button onClick={() => setActiveTab('dashboard')} className="h-16 px-5 flex items-center gap-3 text-left" title="shorts maxxing">
        <img src="/logo.svg" alt="" className="w-8 h-8 shrink-0 rounded-[9px] shadow-[0_0_18px_var(--color-glow)]" />
        <span className="hidden lg:block min-w-0">
          <span className="block font-display lowercase text-xl leading-none text-ink">
            shorts <span className="text-brass italic">maxxing</span>
          </span>
          <span className="block readout mt-1">clip studio</span>
        </span>
      </button>

      <nav className="flex-1 px-3 py-4 space-y-1" aria-label="sections">
        {navItems.map((item) => {
          const NavIcon = item.icon;
          const isActive = activeTab === item.id;
          return (
            <button
              key={item.id}
              onClick={() => setActiveTab(item.id)}
              title={item.label}
              aria-current={isActive ? 'page' : undefined}
              className={`relative w-full flex items-center gap-3 px-3 py-2.5 rounded-input transition-colors ${isActive ? 'bg-paper3 text-ink' : 'text-muted hover:text-ink2 hover:bg-paper3/50'}`}
            >
              {isActive && <span className="absolute left-0 top-2 bottom-2 w-0.5 bg-brass rounded-full" aria-hidden="true" />}
              <NavIcon size={18} className={`shrink-0 mx-auto lg:mx-0 ${isActive ? 'text-brass' : ''}`} />
              <span className="hidden lg:block min-w-0 text-left flex-1">
                <span className="block text-sm lowercase truncate">{item.label}</span>
                <span className="block text-[11px] text-muted truncate">{item.hint}</span>
              </span>
              {item.badge && (
                <span className="absolute top-1.5 right-2 lg:static min-w-[18px] h-[18px] px-1 rounded-full bg-brass text-[10px] font-mono text-paper flex items-center justify-center">
                  {item.badge}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      <div className="p-3 border-t border-rule">
        <div className="hidden lg:block"><ClaudeStatus /></div>
        <div className="lg:hidden flex justify-center py-2"><ClaudeStatus compact /></div>
      </div>
    </aside>
  );

  const MobileTabBar = () => (
    <nav className="md:hidden shrink-0 border-t border-rule bg-paper2/95 backdrop-blur-sm safe-bottom" aria-label="sections">
      <div className="flex items-stretch">
        {navItems.map((item) => {
          const NavIcon = item.icon;
          const isActive = activeTab === item.id;
          return (
            <button
              key={item.id}
              onClick={() => setActiveTab(item.id)}
              aria-current={isActive ? 'page' : undefined}
              className={`flex-1 min-w-0 flex flex-col items-center justify-center gap-1 py-2 min-h-[56px] transition-colors ${isActive ? 'text-ink' : 'text-muted active:text-ink2'}`}
            >
              <NavIcon size={19} className={isActive ? 'text-brass' : ''} />
              <span className="text-[10.5px] lowercase leading-none truncate max-w-full px-0.5">{item.label}</span>
            </button>
          );
        })}
      </div>
    </nav>
  );

  const Banner = ({ icon, title, text, onClose, role }) => {
    const BannerIcon = icon;
    return (
    <div role={role} className="mx-3 sm:mx-6 mt-3 px-4 py-3 bg-paper2 border border-rule rounded-card flex items-start justify-between gap-3 animate-fade shrink-0">
      <div className="flex items-start sm:items-center gap-2 text-sm text-ink2 flex-wrap min-w-0">
        <BannerIcon size={16} className="text-brass shrink-0 mt-0.5 sm:mt-0" />
        <span className="font-medium">{title}</span>
        <span className="text-muted text-xs">{text}</span>
      </div>
      <button onClick={onClose} aria-label="dismiss" className="text-muted hover:text-ink transition-colors shrink-0 -m-1 p-1">
        <X size={16} />
      </button>
    </div>
    );
  };

  return (
    <div className="flex h-screen supports-[height:100dvh]:h-[100dvh] bg-paper overflow-hidden">
      <Sidebar />

      <main className="flex-1 min-w-0 flex flex-col h-full overflow-hidden relative">
        <header className="h-16 border-b border-rule bg-paper flex items-center justify-between gap-2 px-4 sm:px-6 shrink-0 z-10">
          <div className="flex items-center gap-3 min-w-0">
            <img src="/logo.svg" alt="shorts maxxing" className="md:hidden w-7 h-7 rounded-[8px] shrink-0" />
            <h1 className="font-display lowercase text-xl text-ink truncate">{activeNav?.label}</h1>
            {activeTab === 'dashboard' && status !== 'idle' && (
              <button onClick={handleReset} className="btn-ghost px-3.5 py-1.5 text-xs shrink-0" aria-label="new video">
                <Plus size={14} /> <span className="hidden sm:inline">new video</span>
              </button>
            )}
            {activeTab === 'dashboard' && status !== 'idle' && allJobs && (
              <button onClick={() => setActiveTab('queue')} className="btn-ghost px-3.5 py-1.5 text-xs shrink-0" aria-label="queue">
                <ListOrdered size={14} /> <span className="hidden sm:inline">queue{queueBusy ? ` · ${activeJobs.length}` : ''}</span>
              </button>
            )}
          </div>
          <div className="flex items-center gap-3 shrink-0">
            <span className="md:hidden"><ClaudeStatus compact /></span>
            <ProfileSelector profiles={userProfiles} selectedUserId={uploadUserId} onSelect={setUploadUserId} />
          </div>
        </header>

        {sessionRecovered && (
          <Banner icon={RotateCcw} title="Back where you left off" text="Your last video and its clips are open again." onClose={() => setSessionRecovered(false)} />
        )}
        {queuedNotice && (
          <Banner role="status" icon={ListOrdered} title="Added to the queue"
            text="It starts on its own when the videos ahead of it finish. Add another, or follow it on the queue page."
            onClose={() => setQueuedNotice(false)} />
        )}
        {jobGone && (
          <Banner role="status" icon={History} title="That video is no longer on the server"
            text="Finished jobs are cleared after the retention window. Start a new one to keep going." onClose={() => setJobGone(false)} />
        )}

        <div className="flex-1 overflow-hidden relative">

          {/* Settings */}
          {activeTab === 'queue' && (
            <QueuePage jobs={allJobs} onOpen={openJob} refresh={refreshJobs}
              onNew={() => { handleReset(); setActiveTab('dashboard'); }} />
          )}

          {activeTab === 'settings' && (
            <div className="h-full overflow-y-auto custom-scrollbar animate-fade">
              <div className="max-w-2xl mx-auto px-4 py-6 sm:p-8">
                <div className="card">
                  <SettingsSection
                    eyebrow="AI"
                    title="claude does the thinking"
                    badge={<span className={server.online === false ? 'badge-danger' : 'badge-ok'}>{server.online === false ? 'offline' : 'ready'}</span>}
                  >
                    <p className="text-sm text-muted leading-relaxed">
                      Moment picking, layouts, hooks, edits, titles and thumbnails all run on
                      {' '}<span className="text-ink2 font-mono">{model}</span> through the <code className="font-mono text-ink2">claude</code> CLI
                      signed in on this machine, on your Claude subscription. No API key.
                      Pick the model for each step below.
                    </p>
                    {jobRetentionSeconds > 0 && (
                      <p className="text-xs text-muted mt-3 flex items-center gap-1.5">
                        <Clock size={12} /> Finished jobs are kept {formatRetention(jobRetentionSeconds)} (JOB_RETENTION_SECONDS).
                      </p>
                    )}
                  </SettingsSection>

                  {allJobs && (
                    <SettingsSection eyebrow="Models" title="which claude does what">
                      <ModelPicker />
                    </SettingsSection>
                  )}

                  {allJobs && (
                    <SettingsSection eyebrow="Usage" title="tokens spent">
                      <p className="text-sm text-muted leading-relaxed mb-4">
                        Every Claude call counts against your plan&apos;s usage limit. Each job shows its own
                        breakdown too.
                      </p>
                      <UsageSummary />
                    </SettingsSection>
                  )}

                  <SettingsSection
                    eyebrow="Publishing"
                    title="post to tiktok, reels and shorts"
                    badge={profilesState === 'ok' || userProfiles.length
                      ? <span className="badge-ok">connected</span>
                      : <span className="readout">optional</span>}
                  >
                    <p className="text-sm text-muted leading-relaxed mb-4">
                      Posting and scheduling go through <a href="https://app.upload-post.com" target="_blank" rel="noopener noreferrer" className="text-ink2 underline underline-offset-2 hover:text-brass">Upload-Post</a>:
                      connect your accounts there, then paste its API key here.
                    </p>
                    <KeyField id="upload-post" label="Upload-Post API key" value={uploadPostKey} onChange={setUploadPostKey}
                      placeholder="ey…" saveLabel="connect" saved={keySaved === 'upload-post'}
                      onSave={() => { localStorage.setItem('uploadPostKey_v3', encrypt(uploadPostKey)); fetchUserProfiles(); saveKey('upload-post'); }} />
                    {profilesState === 'loading' && <p className="text-xs text-muted mt-2 flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> checking the key…</p>}
                    {profilesState === 'none' && <p className="text-xs text-warn mt-2">The key works, but it has no profiles yet: create one in Upload-Post.</p>}
                    {profilesState === 'error' && <p className="text-xs text-danger mt-2">Upload-Post did not accept this key.</p>}
                    {selectedProfile && (
                      <div className="mt-4 flex items-center justify-between gap-3 rounded-input border border-rule bg-paper px-3 py-2.5">
                        <span className="text-sm text-ink2 truncate">posting as <span className="text-ink">{selectedProfile.username}</span></span>
                        <NetworkIcons connected={selectedProfile.connected || []} size={13} />
                      </div>
                    )}
                  </SettingsSection>

                  {allJobs && (
                    <SettingsSection eyebrow="YouTube" title="upload shorts straight to your channel">
                      <YouTubeSettings />
                    </SettingsSection>
                  )}

                  <SettingsSection eyebrow="Dubbing" title="dub clips into other languages" badge={<span className="readout">optional</span>}>
                    <p className="text-sm text-muted leading-relaxed mb-4">
                      The <span className="text-ink2">dub voice</span> button on a clip uses <a href="https://elevenlabs.io/app/settings/api-keys" target="_blank" rel="noopener noreferrer" className="text-ink2 underline underline-offset-2 hover:text-brass">ElevenLabs</a> (billed
                      by them), then re-captions the clip in the new language.
                    </p>
                    <KeyField id="elevenlabs" label="ElevenLabs API key" value={elevenLabsKey} onChange={setElevenLabsKey}
                      placeholder="sk_…" saveLabel="save" saved={keySaved === 'elevenlabs'}
                      onSave={() => { localStorage.setItem('elevenLabsKey_v1', encrypt(elevenLabsKey)); saveKey('elevenlabs'); }} />
                  </SettingsSection>
                </div>
                <p className="text-xs text-muted mt-4 px-1">
                  Keys stay in this browser and are sent to your backend only with the request that needs them.
                </p>
              </div>
            </div>
          )}

          {activeTab === 'thumbnails' && (
            <ThumbnailStudio
              uploadPostKey={uploadPostKey}
              uploadUserId={uploadUserId}
              onCreateClips={(sessionId) => {
                setActiveTab('dashboard');
                handleProcess({ type: 'thumbnail_session', payload: sessionId, acknowledged: true });
              }}
            />
          )}

          {/* Clips: nothing running yet */}
          {activeTab === 'dashboard' && status === 'idle' && (
            <div className="h-full overflow-y-auto custom-scrollbar animate-fade">
              <div className="max-w-5xl mx-auto px-4 py-6 sm:px-8 sm:py-8 lg:py-10">
                <div className="grid lg:grid-cols-[minmax(0,1fr)_300px] gap-8 lg:gap-12 items-start">
                  <div className="min-w-0">
                    <div className="mb-6">
                      <h2 className="font-display lowercase text-[2.1rem] sm:text-[2.9rem] leading-[1.02] text-ink">
                        turn a long video into shorts
                      </h2>
                      <p className="text-muted text-[15px] sm:text-base leading-relaxed mt-3 max-w-lg">
                        Drop a video or paste a link. Claude finds the moments worth posting and cuts
                        3 to 15 vertical clips, captioned and ready to edit.
                      </p>
                    </div>
                    <MediaInput onProcess={handleProcess} isProcessing={status === 'processing'}
                      submitLabel={queueBusy ? 'add to queue' : 'generate clips'} />
                    <JobList jobs={allJobs} onOpen={openJob} onViewAll={() => setActiveTab('queue')} />
                  </div>

                  <aside className="lg:pt-[8.5rem]" aria-label="what happens">
                    <p className="eyebrow mb-4">what happens</p>
                    <ol className="relative">
                      {PIPELINE.map((step, i) => {
                        const StepIcon = step.icon;
                        return (
                          <li key={step.title} className="relative flex gap-4 pb-6 last:pb-0">
                            {i < PIPELINE.length - 1 && (
                              <span className="flow-line absolute left-[17px] top-10 bottom-1 w-px" style={{ '--flow-delay': `${i * 0.8}s` }} aria-hidden="true" />
                            )}
                            <span className="w-9 h-9 rounded-full border border-rule2 bg-paper2 flex items-center justify-center shrink-0">
                              <StepIcon size={15} className="text-brass" />
                            </span>
                            <span className="pt-1.5 min-w-0">
                              <span className="block text-sm text-ink lowercase">{step.title}</span>
                              <span className="block text-xs text-muted leading-relaxed mt-1">{step.text}</span>
                            </span>
                          </li>
                        );
                      })}
                    </ol>
                    <div className="mt-6 pt-5 border-t border-rule flex items-center justify-between gap-3">
                      <span className="readout">post to</span>
                      <span className="flex items-center gap-3 text-muted">
                        <TikTokIcon size={15} /><Instagram size={15} /><Youtube size={16} />
                      </span>
                    </div>
                  </aside>
                </div>
              </div>
            </div>
          )}

          {/* Clips: running or finished */}
          {activeTab === 'dashboard' && status !== 'idle' && (
            <div className="h-full flex flex-col md:flex-row gap-3 md:gap-4 p-3 md:p-4 overflow-y-auto md:overflow-y-hidden custom-scrollbar animate-fade">

              {/* Source and progress */}
              <div className={`${status === 'complete' ? 'w-full md:w-[30%] lg:w-[25%]' : 'w-full md:w-[55%] lg:w-[60%]'} md:h-full flex flex-col shrink-0 md:shrink card p-4 sm:p-5 md:overflow-y-auto custom-scrollbar transition-all duration-700 ease-in-out`}>
                <div className="mb-4 flex items-center justify-between gap-2">
                  <p className="eyebrow">source</p>
                  <div className="flex items-center gap-2">
                    {status === 'processing' && jobId && (
                      <button
                        type="button"
                        onClick={stopJob}
                        disabled={stopping}
                        className="btn-ghost text-xs py-1 px-2.5 flex items-center gap-1 disabled:opacity-50"
                      >
                        <X size={12} /> {stopping ? 'stopping…' : 'stop'}
                      </button>
                    )}
                    {status !== 'processing' && jobId && allJobs && (
                      <button type="button" className="btn-ghost text-xs py-1 px-2.5 flex items-center gap-1 hover:!border-danger"
                        onClick={async () => { if (await deleteJob({ job_id: jobId })) { handleReset(); refreshJobs(); } }}>
                        <Trash2 size={12} /> delete
                      </button>
                    )}
                    <span className={status === 'processing' ? 'badge-brass' : status === 'complete' ? 'badge-ok' : 'badge-danger'}>
                      {status === 'processing' && <span className="dot dot-ok !bg-brass !shadow-none animate-pulse" />}
                      {STATUS_LABEL[status] || status}
                    </span>
                  </div>
                </div>

                {partialJob?.start_seconds != null && (
                  <p className="mb-3 readout normal-case tracking-normal flex items-center gap-1.5">
                    <Scissors size={12} className="text-brass" />
                    clipping {Math.floor(partialJob.start_seconds / 60)}:{String(Math.round(partialJob.start_seconds % 60)).padStart(2, '0')}
                    {' – '}
                    {Math.floor(partialJob.end_seconds / 60)}:{String(Math.round(partialJob.end_seconds % 60)).padStart(2, '0')}
                  </p>
                )}

                {status === 'processing' && queueInfo && (
                  <div className="mb-4 rounded-input border border-rule bg-paper px-4 py-3 text-sm text-ink2">
                    {queueInfo.ahead === 0
                      ? 'Next in line, starting in a moment…'
                      : <>Waiting for a free slot: <b className="text-ink">#{queueInfo.position}</b> in line.</>}
                  </div>
                )}

                {processingMedia && (
                  <ProcessingAnimation
                    media={processingMedia}
                    isComplete={status === 'complete'}
                    syncedTime={syncedTime}
                    isSyncedPlaying={isSyncedPlaying}
                    syncTrigger={syncTrigger}
                    stage={stage}
                  />
                )}

                {status === 'processing' && (
                  <div className="sm:hidden mb-3 flex items-start gap-2 text-xs text-ink2 min-w-0">
                    <Loader2 size={14} className="animate-spin text-brass shrink-0 mt-px" />
                    <span className="min-w-0 leading-snug break-words">{stage || 'starting up…'}</span>
                  </div>
                )}

                {status === 'error' && jobError && (
                  <div className="mb-3 rounded-input border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-ink2" role="alert">
                    {jobError}
                  </div>
                )}

                {jobUsage?.calls > 0 && (
                  <div className="mb-3"><UsageCard usage={jobUsage} title="claude usage for this job" compact={status !== 'complete'} /></div>
                )}

                <LogPanel logs={logs} times={logTimes} running={status === 'processing'}
                  open={logsVisible} onToggle={() => setLogsVisible(!logsVisible)}
                  className={status === 'complete' ? (logsVisible ? 'h-72' : '') : (logsVisible ? 'flex-1 min-h-[220px]' : '')} />
              </div>

              {/* Clips */}
              <div className={`${status === 'complete' ? 'w-full md:w-[70%] lg:w-[75%]' : 'w-full md:w-[45%] lg:w-[40%]'} md:h-full flex flex-col shrink-0 md:shrink card p-4 sm:p-5 transition-all duration-700 ease-in-out`}>
                <div className="mb-4 shrink-0 space-y-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="font-display lowercase text-xl text-ink mr-auto">clips</h2>
                    {clipsMaking ? (
                      <span className="readout bg-paper3 px-2.5 py-1 rounded-full text-brass">{progress.ready}/{progress.total} ready</span>
                    ) : results?.clips?.length > 0 && (
                      <span className="readout bg-paper3 px-2.5 py-1 rounded-full">{results.clips.length} clips · best first</span>
                    )}
                    {results?.clips?.length > 0 && status === 'complete' && (
                      <>
                        <button onClick={handleDownloadAll} disabled={downloadingAll} className="btn-ghost px-3.5 py-1.5 text-xs" title="Download every clip as a ZIP">
                          {downloadingAll ? <><Loader2 size={14} className="animate-spin" /> zipping…</> : <><Download size={14} /> download all</>}
                        </button>
                        {results.clips.length > 1 && (
                          <button onClick={() => setShowScheduleWeek(true)} className="btn-primary px-4 py-1.5 text-xs">
                            <Calendar size={14} /> schedule a week
                          </button>
                        )}
                      </>
                    )}
                  </div>
                  {clipsMaking && (
                    <div className="h-1 rounded-full bg-paper3 overflow-hidden" role="progressbar"
                      aria-valuemin={0} aria-valuemax={progress.total} aria-valuenow={progress.ready} aria-label="Clips ready">
                      <div className="h-full bg-brass transition-all duration-500" style={{ width: `${(progress.ready / progress.total) * 100}%` }} />
                    </div>
                  )}
                  {status === 'complete' && results?.clips?.length > 0 && jobRetentionSeconds > 0 && (
                    <p className="text-xs text-muted flex items-center gap-1.5">
                      <Clock size={12} /> Kept for {formatRetention(jobRetentionSeconds)}, then cleared: download what you want to keep.
                    </p>
                  )}
                  {status === 'complete' && results?.clips?.length > 0 && !uploadPostKey && (
                    <button onClick={() => setActiveTab('settings')} className="text-xs text-muted hover:text-ink2 transition-colors flex items-center gap-1.5">
                      <Share2 size={12} className="text-brass" /> Add your Upload-Post key in settings to post or schedule from here.
                    </button>
                  )}
                </div>

                <div className="flex-1 overflow-y-auto custom-scrollbar p-1">
                  {results && results.clips && results.clips.length > 0 ? (
                    <div className={`grid gap-4 pb-10 ${status === 'complete' ? 'grid-cols-[repeat(auto-fill,minmax(min(100%,600px),1fr))]' : 'grid-cols-1'}`}>
                      {rankedClips.map(({ clip, index: i }) => (
                        <ResultCard
                          key={`${jobId}-${i}-${clip.video_url || ''}`}
                          clip={clip}
                          index={i}
                          jobId={jobId}
                          onEditClip={(index) => setEditingClip(index)}
                          onReframeClip={(index) => setReframingClip(index)}
                          onModeChanged={status === 'processing' ? null : handleClipRerendered}
                          uploadPostKey={uploadPostKey}
                          uploadUserId={uploadUserId}
                          elevenLabsKey={elevenLabsKey}
                          connectedPlatforms={selectedProfile?.connected ?? null}
                          onPlay={(time) => handleClipPlay(time)}
                          onPause={handleClipPause}
                          onBulkSubtitle={handleBulkSubtitles}
                          clipCount={results.clips.length}
                          bulkProgress={bulkSub}
                          youtube={youtubePosted[String(i)] || null}
                        />
                      ))}
                    </div>
                  ) : status === 'processing' ? (
                    <div className="h-full min-h-[160px] flex flex-col items-center justify-center text-muted space-y-3 text-center px-4">
                      <Loader2 size={26} className="animate-spin text-brass" />
                      <p className="text-sm lowercase text-ink2">
                        {clipsMaking ? `making ${progress.total} ${progress.total === 1 ? 'clip' : 'clips'}…` : `${stage || 'reading the video'}…`}
                      </p>
                      <p className="text-xs max-w-[28ch] leading-snug">They appear here one by one as each finishes rendering.</p>
                    </div>
                  ) : status === 'error' ? (
                    <div className="h-full min-h-[160px] flex flex-col items-center justify-center gap-3 text-center">
                      <p className="text-sm text-danger">No clips this time.</p>
                      <button onClick={handleReset} className="btn-ghost px-4 py-2 text-xs"><Plus size={14} /> try another video</button>
                    </div>
                  ) : null}
                </div>
              </div>
            </div>
          )}
        </div>

        <MobileTabBar />
      </main>

      <ScheduleWeekModal
        isOpen={showScheduleWeek}
        onClose={() => setShowScheduleWeek(false)}
        clips={results?.clips || []}
        jobId={jobId}
        uploadPostKey={uploadPostKey}
        uploadUserId={uploadUserId}
      />

      {qualityGate && (
        <Modal isOpen={true} onClose={() => setQualityGate(null)} size="md" eyebrow="HEADS UP" title="low source quality">
          <div className="space-y-4">
            <p className="text-sm text-ink2">
              YouTube only offers <span className="text-brass font-semibold">{qualityGate.info.max_height}p</span> for this video
              (below the {qualityGate.info.min_height}p recommended). Processing anyway gives lower-quality clips.
            </p>
            {qualityGate.info.cookies_invalid && (
              <p className="text-xs text-muted">
                Your YouTube cookies look expired: exporting them again from an incognito window often unlocks HD.
              </p>
            )}
            <div className="flex gap-2 justify-end pt-2">
              <button onClick={() => setQualityGate(null)} className="btn-ghost">cancel</button>
              <button onClick={() => { const d = qualityGate.data; setQualityGate(null); handleProcess(d, true); }} className="btn-primary">
                process anyway
              </button>
            </div>
          </div>
        </Modal>
      )}

      {editingClip !== null && results?.clips?.[editingClip] && (
        <ClipEditor
          jobId={jobId}
          clipIndex={editingClip}
          clipTitle={results.clips[editingClip].video_title_for_youtube_short || ''}
          onClose={() => setEditingClip(null)}
          onRerendered={handleClipRerendered}
        />
      )}
      {reframingClip !== null && results?.clips?.[reframingClip] && (
        <ReframeEditor
          jobId={jobId}
          clipIndex={reframingClip}
          clipTitle={results.clips[reframingClip].video_title_for_youtube_short || ''}
          onClose={() => setReframingClip(null)}
          onReframed={handleClipRerendered}
        />
      )}
    </div>
  );
}

export default App;
