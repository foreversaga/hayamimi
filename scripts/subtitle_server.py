"""Local subtitle overlay, dashboard, transcript and SSE server."""
from __future__ import annotations

import http.server
import json
import queue
import threading


OVERLAY_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html, body { margin: 0; background: transparent; overflow: hidden; }
  #box {
    position: absolute; left: 2vw; right: 2vw; bottom: 4vh; text-align: center;
    font-family: system-ui, "Noto Sans CJK TC", "Microsoft JhengHei", sans-serif;
    color: #fff; text-shadow: 0 0 8px #000, 2px 2px 3px #000;
  }
  #source { font-size: 3.0vh; line-height: 1.4; opacity: .78; margin-bottom: .35em; }
  #translations { font-size: 4.8vh; line-height: 1.35; font-weight: 700; }
  .translation-line { margin-top: .12em; }
  .lang { font-size: .36em; opacity: .65; margin-right: .6em; vertical-align: .25em; }
  .spec { opacity: .62; font-weight: 500; }
</style>
<div id="box">
  <div id="source"></div>
  <div id="translations"></div>
</div>
<script>
  const source = document.getElementById("source");
  const translations = document.getElementById("translations");
  let activeSegment = null;
  let clearTimer = null;
  const rows = new Map();

  function scheduleClear(ms=7000) {
    if (clearTimer) clearTimeout(clearTimer);
    clearTimer = setTimeout(() => {
      source.textContent = ""; translations.textContent = ""; rows.clear(); activeSegment = null;
    }, ms);
  }

  function row(lang) {
    if (rows.has(lang)) return rows.get(lang);
    const el = document.createElement("div"); el.className = "translation-line";
    const badge = document.createElement("span"); badge.className = "lang"; badge.textContent = lang;
    const committed = document.createElement("span");
    const speculative = document.createElement("span"); speculative.className = "spec";
    el.append(badge, committed, speculative); translations.appendChild(el);
    const value = {el, committed, speculative}; rows.set(lang, value); return value;
  }

  function resetFor(segmentId) {
    if (!segmentId || activeSegment === segmentId) return;
    activeSegment = segmentId; translations.textContent = ""; rows.clear();
  }

  const es = new EventSource("/events");
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "partial") {
      resetFor(ev.segment_id); source.textContent = ev.text || ""; scheduleClear();
    } else if (ev.type === "final") {
      resetFor(ev.segment_id); source.textContent = ev.text || ""; scheduleClear();
    } else if (ev.type === "translation_partial") {
      resetFor(ev.segment_id);
      const r = row(ev.lang || "?");
      r.committed.textContent = ev.committed || "";
      r.speculative.textContent = ev.speculative || "";
      scheduleClear();
    } else if (ev.type === "translation_final") {
      resetFor(ev.segment_id);
      const r = row(ev.lang || "?");
      r.committed.textContent = ev.text || ""; r.speculative.textContent = "";
      scheduleClear(8000);
    } else if (ev.type === "translation") {
      const r = row(ev.lang || "?"); r.committed.textContent = ev.text || "";
      r.speculative.textContent = ""; scheduleClear(8000);
    }
  };
</script>
</html>
"""


TRANSCRIPT_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hayamimi transcript</title>
<style>
  body { margin: 0; padding: 1.5rem; background: #101217; color: #eee;
    font-family: system-ui, "Noto Sans CJK TC", sans-serif; line-height: 1.7; }
  .entry { padding: .65rem 0; border-bottom: 1px solid #292e39; }
  .meta { color: #8f98aa; font-size: .75rem; margin-right: .6rem; }
  .translation { color: #d9c9a9; margin-left: 1.5rem; }
</style>
<div id="lines"></div>
<script>
  const box = document.getElementById("lines");
  const refined = new Map();
  function add(text, meta, cls="") {
    const p = document.createElement("div"); p.className = "entry " + cls;
    const m = document.createElement("span"); m.className = "meta"; m.textContent = meta;
    p.append(m, document.createTextNode(text)); box.appendChild(p);
    window.scrollTo(0, document.body.scrollHeight); return p;
  }
  const es = new EventSource("/events");
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "refine") {
      const el = add(ev.text || "", `[${ev.speaker ? ev.speaker + " · " : ""}${ev.lang || "?"}]`);
      if (ev.segment_id) refined.set(ev.segment_id, el);
    } else if (ev.type === "translation_refine") {
      add(ev.text || "", `[→${ev.lang || "?"}]`, "translation");
    }
  };
</script>
</html>
"""


DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hayamimi</title>
<style>
  :root { --bg:#0d0f13; --panel:#151922; --line:#2a303c; --text:#eee9df;
    --muted:#929bad; --accent:#e05b3e; --translation:#e1c690; }
  * { box-sizing: border-box; }
  html,body { height:100%; }
  body { margin:0; background:var(--bg); color:var(--text); font-family:system-ui,"Noto Sans CJK TC",sans-serif;
    display:flex; flex-direction:column; }
  header { display:flex; gap:1rem; align-items:center; padding:.8rem 1.2rem; border-bottom:1px solid var(--line); }
  h1 { font-size:1.15rem; margin:0; letter-spacing:.18em; } h1 b { color:var(--accent); }
  .spacer { flex:1; } .chip { color:var(--muted); font-size:.78rem; border:1px solid var(--line); padding:.25rem .55rem; border-radius:4px; }
  #partial-panel { padding:1rem 1.2rem; background:var(--panel); border-bottom:1px solid var(--line); min-height:7.2rem; }
  .label { color:var(--muted); font-size:.68rem; letter-spacing:.16em; text-transform:uppercase; }
  #partial-source { font-size:1.35rem; margin-top:.35rem; min-height:2rem; }
  #partial-translations { color:var(--translation); font-size:1.2rem; margin-top:.45rem; }
  .partial-tr { margin-top:.2rem; } .partial-tr .spec { opacity:.55; }
  main { flex:1; min-height:0; display:grid; grid-template-columns:3fr 2fr; }
  section { min-height:0; display:flex; flex-direction:column; } section+section { border-left:1px solid var(--line); }
  h2 { margin:0; padding:.65rem 1rem; font-size:.72rem; color:var(--muted); border-bottom:1px solid var(--line); }
  .scroll { overflow:auto; padding:.6rem 1rem 2rem; }
  .card { padding:.7rem 0; border-bottom:1px dashed var(--line); }
  .meta { display:flex; gap:.45rem; color:var(--muted); font-size:.68rem; margin-bottom:.28rem; }
  .badge { color:#111; background:var(--accent); padding:.08rem .35rem; border-radius:3px; font-weight:700; }
  .source { font-size:1rem; line-height:1.55; }
  .tr { margin:.35rem 0 0 1.2rem; color:var(--translation); line-height:1.5; }
  .tr b { color:var(--accent); font-size:.72rem; margin-right:.5rem; }
  .refine { padding:.65rem 0; border-bottom:1px dashed var(--line); line-height:1.6; }
  .error { color:#ff8a80; font-size:.78rem; margin-left:1.2rem; }
  footer { padding:.4rem 1rem; border-top:1px solid var(--line); color:var(--muted); font-size:.7rem; }
  @media(max-width:850px){main{grid-template-columns:1fr}section+section{border-left:0;border-top:1px solid var(--line)}}
</style>
<header><h1>早<b>耳</b> hayamimi</h1><div class="spacer"></div>
<div class="chip">final <b id="n-finals">0</b></div><div class="chip">ASR avg <b id="mean-lat">-</b> ms</div>
<div class="chip" id="conn">connecting</div></header>
<div id="partial-panel"><div class="label">live source</div><div id="partial-source">waiting for audio…</div>
<div id="partial-translations"></div></div>
<main><section><h2>FINAL + TRANSLATION</h2><div class="scroll" id="feed"></div></section>
<section><h2>REFINED TRANSCRIPT</h2><div class="scroll" id="refined"></div></section></main>
<footer>overlay / · dashboard /dashboard · transcript /transcript · health /healthz</footer>
<script>
  const feed=document.getElementById("feed"), refined=document.getElementById("refined");
  const partialSource=document.getElementById("partial-source"), partialTr=document.getElementById("partial-translations");
  const cards=new Map(), partialRows=new Map(); let activeSegment=null, finals=0, latSum=0;
  function stick(el){el.scrollTop=el.scrollHeight}
  function clearPartials(segmentId){ if(segmentId && activeSegment!==segmentId){activeSegment=segmentId;partialTr.textContent="";partialRows.clear();} }
  function partialRow(lang){if(partialRows.has(lang))return partialRows.get(lang);const d=document.createElement("div");d.className="partial-tr";
    const b=document.createElement("b");b.textContent="→"+lang+" ";const c=document.createElement("span"),s=document.createElement("span");s.className="spec";
    d.append(b,c,s);partialTr.appendChild(d);const v={c,s};partialRows.set(lang,v);return v;}
  function makeCard(ev){const card=document.createElement("div");card.className="card";card.dataset.segment=ev.segment_id||"";
    const meta=document.createElement("div");meta.className="meta";const badge=document.createElement("span");badge.className="badge";badge.textContent=ev.lang||"?";
    meta.appendChild(badge);if(ev.speaker){const sp=document.createElement("span");sp.textContent=ev.speaker;meta.appendChild(sp)}
    const lat=document.createElement("span");lat.textContent=ev.latency_ms!=null?Math.round(ev.latency_ms)+"ms":"";meta.appendChild(lat);
    const src=document.createElement("div");src.className="source";src.textContent=ev.text||"";card.append(meta,src);feed.appendChild(card);
    if(ev.segment_id)cards.set(ev.segment_id,card);stick(feed);return card;}
  function attachTranslation(ev,kind="tr"){const card=cards.get(ev.segment_id);if(!card)return;let el=card.querySelector(`[data-tr-lang="${ev.lang}"]`);
    if(!el){el=document.createElement("div");el.className=kind;el.dataset.trLang=ev.lang;const b=document.createElement("b");b.textContent="→"+ev.lang;const t=document.createElement("span");el.append(b,t);card.appendChild(el)}
    el.querySelector("span").textContent=ev.text||"";stick(feed);}
  const es=new EventSource("/events"); es.onopen=()=>document.getElementById("conn").textContent="connected";
  es.onerror=()=>document.getElementById("conn").textContent="reconnecting";
  es.onmessage=(e)=>{const ev=JSON.parse(e.data);
    if(ev.type==="partial"){clearPartials(ev.segment_id);partialSource.textContent=ev.text||"";}
    else if(ev.type==="translation_partial"){clearPartials(ev.segment_id);const r=partialRow(ev.lang||"?");r.c.textContent=ev.committed||"";r.s.textContent=ev.speculative||"";}
    else if(ev.type==="final"){clearPartials(ev.segment_id);partialSource.textContent=ev.text||"";makeCard(ev);finals++;if(ev.latency_ms!=null)latSum+=ev.latency_ms;
      document.getElementById("n-finals").textContent=finals;document.getElementById("mean-lat").textContent=Math.round(latSum/finals);}
    else if(ev.type==="translation_final"){attachTranslation(ev);const r=partialRow(ev.lang||"?");r.c.textContent=ev.text||"";r.s.textContent="";}
    else if(ev.type==="translation"){const last=[...cards.values()].at(-1);if(last){const fake={...ev,segment_id:last.dataset.segment};attachTranslation(fake)}}
    else if(ev.type==="translation_error"){const card=cards.get(ev.segment_id);if(card){const x=document.createElement("div");x.className="error";x.textContent=`→${ev.lang}: ${ev.error}`;card.appendChild(x)}}
    else if(ev.type==="refine"){const d=document.createElement("div");d.className="refine";d.dataset.refine=ev.segment_id||"";d.textContent=ev.text||"";refined.appendChild(d);stick(refined);}
    else if(ev.type==="translation_refine"){const d=document.createElement("div");d.className="tr";const b=document.createElement("b");b.textContent="→"+ev.lang;d.append(b,document.createTextNode(ev.text||""));refined.appendChild(d);stick(refined);}
  };
</script>
</html>
"""


class SubtitleServer:
    """Fan-out subtitle events to SSE clients and serve browser views."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self._clients: list[queue.Queue[str]] = []
        self._history: list[str] = []
        self._lock = threading.Lock()
        self._httpd: http.server.ThreadingHTTPServer | None = None

    def publish(self, event: dict) -> None:
        data = json.dumps(event, ensure_ascii=False)
        with self._lock:
            if event.get("type") in ("refine", "translation_refine"):
                self._history.append(data)
                del self._history[:-400]
            for client in list(self._clients):
                if client.full():
                    try:
                        client.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    client.put_nowait(data)
                except queue.Full:
                    pass

    def partial(self, text: str, segment_id: str = "", lang: str = "") -> None:
        self.publish({"type": "partial", "text": text, "segment_id": segment_id, "lang": lang})

    def final(
        self,
        text: str,
        lang: str = "",
        speaker: str = "",
        latency_ms: float | None = None,
        tier: str = "",
        segment_id: str = "",
    ) -> None:
        self.publish({
            "type": "final",
            "text": text,
            "lang": lang,
            "speaker": speaker,
            "latency_ms": latency_ms,
            "tier": tier,
            "segment_id": segment_id,
        })

    def start(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _html(self, body_text: str) -> None:
                body = body_text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/healthz":
                    body = b'{"status":"ok"}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path == "/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    client: queue.Queue[str] = queue.Queue(maxsize=256)
                    with server._lock:
                        for past in server._history:
                            if not client.full():
                                client.put_nowait(past)
                        server._clients.append(client)
                    try:
                        while True:
                            try:
                                data = client.get(timeout=15)
                                frame = f"data: {data}\n\n".encode("utf-8")
                            except queue.Empty:
                                frame = b": ping\n\n"
                            self.wfile.write(frame)
                            self.wfile.flush()
                    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                        pass
                    finally:
                        with server._lock:
                            if client in server._clients:
                                server._clients.remove(client)
                    return

                if path == "/dashboard":
                    self._html(DASHBOARD_HTML)
                elif path == "/transcript":
                    self._html(TRANSCRIPT_HTML)
                else:
                    self._html(OVERLAY_HTML)

        class ReusableThreadingHTTPServer(http.server.ThreadingHTTPServer):
            allow_reuse_address = True

        self._httpd = ReusableThreadingHTTPServer((self.host, self.port), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self
