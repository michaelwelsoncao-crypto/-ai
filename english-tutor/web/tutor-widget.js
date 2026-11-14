/*
 * Drop-in widget: add ONE line to any web page (made by Codex or anything else):
 *
 *   <script src="http://localhost:8000/static/tutor-widget.js"></script>
 *
 * A floating "和 AI 老师说英语" button appears in the bottom-right corner.
 * Options (optional attributes on the script tag):
 *   data-title="Lily 老师"      panel title
 *   data-position="left"         put the button bottom-left instead
 *   data-open="true"             open the panel immediately
 * Styles live in a Shadow DOM, so they never clash with your page.
 */
(function () {
  const script = document.currentScript;
  const base = new URL(".", script.src);          // .../static/
  const server = new URL("/", script.src).origin; // http://localhost:8000
  const title = script.dataset.title || "AI 英语老师";
  const left = script.dataset.position === "left";

  function loadClient() {
    if (window.EnglishTutor) return Promise.resolve();
    return new Promise((ok, fail) => {
      const s = document.createElement("script");
      s.src = new URL("tutor-client.js", base).toString();
      s.onload = ok;
      s.onerror = () => fail(new Error("加载 tutor-client.js 失败，请确认对话服务已启动：" + server));
      document.head.appendChild(s);
    });
  }

  const host = document.createElement("div");
  host.id = "english-tutor-widget";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
<style>
  :host { all: initial; }
  * { box-sizing: border-box; font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
  .fab { position: fixed; bottom: 20px; ${left ? "left" : "right"}: 20px; z-index: 2147483000;
         border: 0; border-radius: 999px; padding: 14px 20px; font-size: 16px; font-weight: 600;
         background: #f06a3b; color: #fff; box-shadow: 0 6px 20px rgba(0,0,0,.2); cursor: pointer; }
  .panel { position: fixed; bottom: 84px; ${left ? "left" : "right"}: 20px; z-index: 2147483000;
           width: min(380px, calc(100vw - 32px)); height: min(560px, calc(100vh - 120px));
           background: #fff8f0; color: #3a2e2a; border-radius: 18px; box-shadow: 0 10px 40px rgba(0,0,0,.25);
           display: none; flex-direction: column; overflow: hidden; }
  .panel.open { display: flex; }
  header { padding: 12px 16px; background: #f06a3b; color: #fff; font-weight: 600; display: flex; justify-content: space-between; align-items: center; }
  header button { background: none; border: 0; color: #fff; font-size: 20px; cursor: pointer; }
  .status { padding: 6px 16px; font-size: 13px; color: #8a7a72; }
  .log { flex: 1; overflow-y: auto; padding: 8px 12px; display: flex; flex-direction: column; gap: 8px; }
  .msg { max-width: 85%; padding: 8px 12px; border-radius: 14px; font-size: 15px; line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
  .kid { align-self: flex-end; background: #ffe2d3; }
  .tutor { align-self: flex-start; background: #e6f2ff; }
  .meta { align-self: center; font-size: 12px; color: #8a7a72; }
  .bar { display: flex; gap: 8px; padding: 12px; }
  .bar button { flex: 1; border: 0; border-radius: 999px; padding: 14px; font-size: 16px; font-weight: 600; cursor: pointer; background: #f06a3b; color: #fff; touch-action: none; user-select: none; }
  .bar button.ghost { background: #fff; color: #3a2e2a; border: 1px solid #c9b8ae; flex: 0 0 auto; padding: 14px 18px; }
  .bar button[hidden] { display: none; }
  .bar button.active { filter: brightness(.85); }
  @media (prefers-color-scheme: dark) {
    .panel { background: #1d1916; color: #f3e9e2; }
    .kid { background: #5a3b2c; } .tutor { background: #22364a; }
    .bar button.ghost { background: #2a2420; color: #f3e9e2; }
  }
</style>
<button class="fab" part="button">🎤 和 AI 老师说英语</button>
<div class="panel" role="dialog">
  <header><span class="title"></span><button class="close" aria-label="关闭">×</button></header>
  <div class="status">点“开始对话”，允许使用麦克风</div>
  <div class="log"></div>
  <div class="bar">
    <button class="start">开始对话</button>
    <button class="ptt" hidden>按住说话</button>
    <button class="stop ghost" hidden>结束</button>
  </div>
</div>`;

  const $ = (sel) => root.querySelector(sel);
  $(".title").textContent = title;
  $(".panel").setAttribute("aria-label", title);
  const panel = $(".panel"), log = $(".log"), startBtn = $(".start"), pttBtn = $(".ptt"), stopBtn = $(".stop");
  let tutor = null, bubble = null;

  const status = (t) => { $(".status").textContent = t; };
  function add(cls, text) {
    const d = document.createElement("div");
    d.className = "msg " + cls;
    d.textContent = text;
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
    return d;
  }
  function idleUi() {
    startBtn.hidden = false; pttBtn.hidden = true; stopBtn.hidden = true;
  }

  function onEvent(ev) {
    switch (ev.type) {
      case "ready":
        pttBtn.hidden = !tutor.pttMode;
        status(tutor.pttMode ? "按住按钮说话，松开后老师回答" : "直接说英语吧，说话可以打断老师");
        break;
      case "speech_started": status("正在听…"); break;
      case "speech_stopped": status("思考中…"); break;
      case "interrupt": bubble = null; break;
      case "user_transcript": if (ev.text) add("kid", ev.text); break;
      case "response_started": bubble = null; status("老师在说…"); break;
      case "text_delta":
        if (!bubble) bubble = add("tutor", "");
        bubble.textContent += ev.text; log.scrollTop = log.scrollHeight;
        break;
      case "response_done":
        bubble = null;
        status(tutor.pttMode ? "按住按钮说话" : "轮到你说啦");
        break;
      case "error": add("meta", "出错了：" + ev.message); break;
      case "closed": status("对话已结束"); idleUi(); break;
    }
  }

  async function start() {
    startBtn.hidden = true;
    status("正在连接…");
    try {
      await loadClient();
      tutor = window.EnglishTutor.create({ server });
      tutor.on("event", onEvent);
      await tutor.start();
      stopBtn.hidden = false;
    } catch (e) {
      add("meta", "启动失败：" + e.message);
      status("启动失败");
      idleUi();
    }
  }

  $(".fab").onclick = () => panel.classList.toggle("open");
  $(".close").onclick = () => { panel.classList.remove("open"); if (tutor) tutor.stop(); };
  startBtn.onclick = start;
  stopBtn.onclick = () => tutor && tutor.stop();
  pttBtn.addEventListener("pointerdown", () => { pttBtn.classList.add("active"); status("正在听…（松开结束）"); tutor.pttStart(); });
  const up = () => { if (!pttBtn.classList.contains("active")) return; pttBtn.classList.remove("active"); status("思考中…"); tutor.pttStop(); };
  pttBtn.addEventListener("pointerup", up);
  pttBtn.addEventListener("pointerleave", up);

  const mount = () => {
    document.body.appendChild(host);
    if (script.dataset.open === "true") panel.classList.add("open");
  };
  if (document.body) mount(); else document.addEventListener("DOMContentLoaded", mount);
})();
