/*
 * EnglishTutor client: microphone -> server -> AI tutor voice. No UI.
 *
 *   const tutor = EnglishTutor.create({ server: "http://localhost:8000" });
 *   tutor.on("event", (ev) => console.log(ev));   // see server/realtime.py for event types
 *   await tutor.start();                          // must be called from a click/tap
 *   tutor.pttStart(); tutor.pttStop();            // only when tutor.pttMode is true
 *   tutor.stop();
 */
(function () {
  const WORKLET = `
class TutorMic extends AudioWorkletProcessor {
  constructor() { super(); this.ratio = sampleRate / 16000; this.pos = 0; this.buf = []; }
  process(inputs) {
    const ch = inputs[0][0];
    if (!ch) return true;
    for (; this.pos < ch.length; this.pos += this.ratio) {
      const s = Math.max(-1, Math.min(1, ch[Math.floor(this.pos)]));
      this.buf.push(s < 0 ? s * 0x8000 : s * 0x7fff);
    }
    this.pos -= ch.length;
    if (this.buf.length >= 640) { this.port.postMessage(Int16Array.from(this.buf).buffer, []); this.buf = []; }
    return true;
  }
}
registerProcessor("tutor-mic", TutorMic);`;

  function wsUrl(server) {
    const u = new URL(server || location.origin, location.href);
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    u.pathname = "/ws/browser";
    return u.toString();
  }

  function create(opts) {
    opts = opts || {};
    const listeners = {};
    let ws = null, ctx = null, mic = null, node = null, playHead = 0, playing = [], pttDown = false;
    const api = {
      pttMode: false,
      running: false,
      on(type, fn) { (listeners[type] = listeners[type] || []).push(fn); return api; },
      start, stop, pttStart, pttStop,
    };
    const emit = (type, data) => (listeners[type] || []).forEach((fn) => fn(data));

    function play(buf) {
      const pcm = new Int16Array(buf);
      const audio = ctx.createBuffer(1, pcm.length, 24000);
      const ch = audio.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 0x8000;
      const src = ctx.createBufferSource();
      src.buffer = audio;
      src.connect(ctx.destination);
      playHead = Math.max(playHead, ctx.currentTime + 0.05);
      src.start(playHead);
      playHead += audio.duration;
      playing.push(src);
      src.onended = () => { playing = playing.filter((s) => s !== src); };
    }
    function stopPlayback() {
      playing.forEach((s) => { try { s.stop(); } catch (e) {} });
      playing = []; playHead = 0;
    }
    function onEvent(ev) {
      if (ev.type === "ready") {
        api.pttMode = ev.turn_mode === "push_to_talk";
        ws.send(JSON.stringify({ type: "greet" }));
      }
      if (ev.type === "interrupt") stopPlayback();
      emit("event", ev);
    }

    async function start() {
      if (api.running) return;
      api.running = true;
      try {
        ctx = new AudioContext();
        mic = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
        await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" })));
        node = new AudioWorkletNode(ctx, "tutor-mic");
        ctx.createMediaStreamSource(mic).connect(node);
        ws = new WebSocket(wsUrl(opts.server));
        ws.binaryType = "arraybuffer";
        ws.onmessage = (m) => (typeof m.data === "string" ? onEvent(JSON.parse(m.data)) : play(m.data));
        ws.onerror = () => emit("event", { type: "error", message: "连不上对话服务器：" + wsUrl(opts.server) });
        ws.onclose = () => { cleanup(); emit("event", { type: "closed" }); };
        node.port.onmessage = (e) => {
          if (ws && ws.readyState === 1 && (!api.pttMode || pttDown)) ws.send(e.data);
        };
      } catch (e) {
        cleanup();
        throw e;
      }
    }
    function cleanup() {
      stopPlayback();
      if (mic) mic.getTracks().forEach((t) => t.stop());
      if (ctx) ctx.close();
      mic = ctx = node = null;
      api.running = false;
    }
    function stop() { if (ws) ws.close(); else cleanup(); }
    function pttStart() {
      if (!ws || ws.readyState !== 1) return;
      pttDown = true; stopPlayback();
      ws.send(JSON.stringify({ type: "ptt_start" }));
    }
    function pttStop() {
      if (!pttDown) return;
      pttDown = false;
      if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "ptt_stop" }));
    }
    return api;
  }

  window.EnglishTutor = { create };
})();
