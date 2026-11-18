import React, { useEffect, useState, useRef } from 'react';
import { Loader2, Play, CheckCircle } from 'lucide-react';
import { getApiUrl } from '../config';
import { apiFetch } from '../lib/api';

const ProcessingAnimation = ({ media, isComplete, syncedTime, isSyncedPlaying, syncTrigger, stage = null }) => {
  const [videoSrc, setVideoSrc] = useState(null);
  const [isYouTube, setIsYouTube] = useState(false);
  const videoRef = useRef(null);
  const iframeRef = useRef(null);

  useEffect(() => {
    if (!media) return;

    if (media.type === 'file') {
      const url = URL.createObjectURL(media.payload);
      setIsYouTube(false);
      setVideoSrc(url);
      return () => URL.revokeObjectURL(url);
    } else if (media.type === 'server') {
      // Uploaded source served from the backend (survives a page reload).
      // The payload is the plain /api/source/<job> path, because that is what
      // gets persisted; the signed URL is minted here, at render, so a stored
      // session never carries a token that has since expired. A <video> tag
      // cannot send the bearer header itself, hence the round trip.
      setIsYouTube(false);
      let cancelled = false;
      const jobId = media.payload.split('/').pop();
      (async () => {
        try {
          const res = await apiFetch(`/api/source-url/${jobId}`);
          const { url } = await res.json();
          if (!cancelled && url) setVideoSrc(getApiUrl(url));
        } catch (e) {
          // Self-host, or a backend without the endpoint: the open path still
          // works there, and losing the preview is worse than an unsigned URL.
          if (!cancelled) setVideoSrc(getApiUrl(media.payload));
        }
      })();
      return () => { cancelled = true; };
    } else if (media.type === 'url') {
      setIsYouTube(true);
      const videoId = getYouTubeId(media.payload);
      setVideoSrc(videoId);
    }
  }, [media]);

  // Handle Sync Playback for Local Video
  useEffect(() => {
    if (!isYouTube && videoRef.current) {
      if (isSyncedPlaying) {
        // Sync Mode: Seek to time and Play. A non-finite time (a clip with no
        // start yet) would throw and take the whole tree down — skip the seek.
        if (Number.isFinite(syncedTime)) videoRef.current.currentTime = syncedTime;
        videoRef.current.play().catch(e => console.log("Auto-play prevented", e));
        videoRef.current.loop = false;
        videoRef.current.muted = true; // Keep muted to avoid double audio with clip
      } else {
        // Stop Sync: Pause. Once analysis is complete, resume the ambient loop.
        videoRef.current.pause();

        if (isComplete) {
             videoRef.current.loop = true;
             videoRef.current.play().catch(e => console.log("Ambient play prevented", e));
        }
      }
    }
  }, [syncedTime, isSyncedPlaying, isYouTube, isComplete, syncTrigger]);

  // Handle Sync Playback for YouTube (Basic Iframe Control via PostMessage)
  useEffect(() => {
    if (isYouTube && iframeRef.current && videoSrc) {
        const iframeWindow = iframeRef.current.contentWindow;
        if (isSyncedPlaying) {
             // Seek and Play
             iframeWindow.postMessage(JSON.stringify({ event: 'command', func: 'seekTo', args: [syncedTime, true] }), '*');
             iframeWindow.postMessage(JSON.stringify({ event: 'command', func: 'playVideo', args: [] }), '*');
        } else {
             // Pause
             iframeWindow.postMessage(JSON.stringify({ event: 'command', func: 'pauseVideo', args: [] }), '*');
        }
    }
  }, [syncedTime, isSyncedPlaying, isYouTube, videoSrc, syncTrigger]);


  const getYouTubeId = (url) => {
    const regExp = /^.*(youtu.be\/|v\/|u\/\w\/|embed\/|watch\?v=|&v=)([^#&?]*).*/;
    const match = url.match(regExp);
    return (match && match[2].length === 11) ? match[2] : null;
  };

  const containerClasses = `relative w-full aspect-[2/1] sm:aspect-video rounded-input overflow-hidden bg-black border border-rule mb-4 animate-fade transition-shadow duration-500
    ${isSyncedPlaying ? 'ring-1 ring-brass ring-offset-2 ring-offset-paper2' : ''}`;

  // Dimmed while the job runs and between clip plays, so the clips stay the
  // focus; full brightness while it follows the clip being played.
  const videoOpacity = isSyncedPlaying ? 'opacity-100' : isComplete ? 'opacity-35' : 'opacity-55';

  return (
    <div className={containerClasses}>
      <div className={`absolute inset-0 transition-opacity duration-700 ${videoOpacity}`}>
        {isYouTube && videoSrc ? (
          <iframe
            ref={iframeRef}
            className={`w-full h-full ${isSyncedPlaying ? '' : 'pointer-events-none'}`}
            // enablejsapi=1 for postMessage control
            src={`https://www.youtube.com/embed/${videoSrc}?autoplay=1&mute=1&controls=0&loop=1&playlist=${videoSrc}&modestbranding=1&showinfo=0&rel=0&enablejsapi=1`}
            title="Source video"
            frameBorder="0"
            allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
          />
        ) : videoSrc ? (
          <video ref={videoRef} src={videoSrc} className="w-full h-full object-cover" autoPlay muted loop playsInline />
        ) : (
          <div className="w-full h-full flex items-center justify-center bg-paper">
            <Loader2 size={22} className="animate-spin text-muted" />
          </div>
        )}
      </div>

      {/* One honest label: the stage the job's own log reports. */}
      <div className="absolute top-2.5 left-2.5 sm:top-3 sm:left-3 z-10 pointer-events-none">
        <span className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-black/70 readout ${isComplete && !isSyncedPlaying ? 'text-ok' : 'text-brass'}`}>
          {isSyncedPlaying
            ? <><Play size={12} /> following the clip</>
            : isComplete
              ? <><CheckCircle size={13} /> done</>
              : <><Loader2 size={12} className="animate-spin" /> {stage || 'starting'}</>}
        </span>
      </div>
    </div>
  );
};

export default ProcessingAnimation;
