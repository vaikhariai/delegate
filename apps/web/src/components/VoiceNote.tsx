import { useEffect, useRef, useState } from "react";
import { blobToWav16k } from "../wav";
import { Icon } from "./Icon";

const MAX_SECONDS = 45;

type Phase = "idle" | "recording" | "encoding" | "error";

/** Records a short voice note and hands back a 16 kHz mono 16-bit WAV. */
export function VoiceNote({ disabled, onWav }: { disabled?: boolean; onWav: (wav: Blob, seconds: number) => void }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [seconds, setSeconds] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const rec = useRef<MediaRecorder | null>(null);
  const timer = useRef<number | null>(null);
  const started = useRef(0);

  const supported = typeof window !== "undefined" && "MediaRecorder" in window && !!navigator.mediaDevices?.getUserMedia;

  useEffect(
    () => () => {
      if (timer.current) window.clearInterval(timer.current);
      if (rec.current && rec.current.state !== "inactive") rec.current.stop();
    },
    [],
  );

  async function start() {
    setError(null);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    } catch {
      setPhase("error");
      setError("Microphone permission was not granted. You can type the description instead.");
      return;
    }
    const chunks: Blob[] = [];
    const r = new MediaRecorder(stream);
    rec.current = r;
    r.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    r.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      if (timer.current) window.clearInterval(timer.current);
      const dur = Math.min(MAX_SECONDS, Math.round((Date.now() - started.current) / 1000));
      setPhase("encoding");
      try {
        const wav = await blobToWav16k(new Blob(chunks, { type: r.mimeType }));
        setPhase("idle");
        onWav(wav, dur);
      } catch {
        setPhase("error");
        setError("Could not convert the recording to WAV in this browser. Type the description instead.");
      }
    };
    started.current = Date.now();
    setSeconds(0);
    r.start(250);
    setPhase("recording");
    timer.current = window.setInterval(() => {
      const s = Math.floor((Date.now() - started.current) / 1000);
      setSeconds(s);
      if (s >= MAX_SECONDS && r.state === "recording") r.stop();
    }, 250);
  }

  function stop() {
    if (rec.current && rec.current.state === "recording") rec.current.stop();
  }

  if (!supported) {
    return <p className="hint">Voice notes need a browser with microphone recording. Type the description instead.</p>;
  }

  return (
    <div className="voice">
      {phase === "recording" ? (
        <button type="button" className="btn btn-danger" onClick={stop} data-testid="voice-stop">
          <Icon name="stop" /> Stop and use this note ({seconds}s of {MAX_SECONDS}s)
        </button>
      ) : (
        <button type="button" className="btn btn-secondary" onClick={() => void start()} disabled={disabled || phase === "encoding"} data-testid="voice-record">
          <Icon name="mic" /> {phase === "encoding" ? "Preparing the recording…" : "Record a voice note"}
        </button>
      )}
      {phase === "recording" ? (
        <span className="rec-indicator" role="status">
          <span className="rec-dot" aria-hidden="true" /> Recording · stops automatically at {MAX_SECONDS} seconds
        </span>
      ) : null}
      {error ? (
        <p className="field-error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
