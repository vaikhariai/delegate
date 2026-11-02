// Browser-side conversion of a MediaRecorder clip to 16 kHz mono 16-bit PCM WAV (what /v1/mandate-drafts/voice accepts).

export const TARGET_RATE = 16000;

export async function blobToWav16k(blob: Blob): Promise<Blob> {
  const buf = await blob.arrayBuffer();
  const Ctor: typeof AudioContext =
    window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
  const ctx = new Ctor();
  let decoded: AudioBuffer;
  try {
    decoded = await ctx.decodeAudioData(buf);
  } finally {
    void ctx.close();
  }
  const samples = await resampleMono(decoded, TARGET_RATE);
  return encodeWav(samples, TARGET_RATE);
}

export async function resampleMono(input: AudioBuffer, rate: number): Promise<Float32Array> {
  const length = Math.max(1, Math.ceil(input.duration * rate));
  const offline = new OfflineAudioContext(1, length, rate);
  const src = offline.createBufferSource();
  src.buffer = input;
  // A single-channel destination downmixes the source (speakers interpretation).
  src.connect(offline.destination);
  src.start(0);
  const rendered = await offline.startRendering();
  return rendered.getChannelData(0);
}

export function encodeWav(samples: Float32Array, rate: number): Blob {
  const bytesPerSample = 2;
  const dataLen = samples.length * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataLen);
  const v = new DataView(buffer);
  const ascii = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i));
  };
  ascii(0, "RIFF");
  v.setUint32(4, 36 + dataLen, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  v.setUint32(16, 16, true); // PCM chunk size
  v.setUint16(20, 1, true); // PCM
  v.setUint16(22, 1, true); // mono
  v.setUint32(24, rate, true);
  v.setUint32(28, rate * bytesPerSample, true);
  v.setUint16(32, bytesPerSample, true);
  v.setUint16(34, 16, true);
  ascii(36, "data");
  v.setUint32(40, dataLen, true);
  let off = 44;
  for (let i = 0; i < samples.length; i++, off += 2) {
    const s = Math.max(-1, Math.min(1, samples[i] ?? 0));
    v.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}
