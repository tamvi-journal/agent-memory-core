"""A local, read-only web view of an agent's identity memory.

`trajecta-identity view` serves one page on 127.0.0.1. Nothing on the page can
write: recall from the page never changes activation.
"""

from __future__ import annotations

import json
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .activation import state_of
from .identity import PINNED, IdentityMemory


def snapshot(memory: IdentityMemory) -> dict[str, Any]:
    if memory.store.schema_info()["state"] == "uninitialized":
        memory.bootstrap()
    rows = [row for row in memory.store.current_view() if row["domain"] != "anchor"]
    nodes = []
    for row in rows:
        nodes.append({
            "record_id": row["record_id"],
            "domain": row["domain"],
            "title": row["title"],
            "summary": row["summary"],
            "revision": row["revision_number"],
            "state": state_of(row, memory.activation, PINNED),
            "accessibility": round(float(row["accessibility"]), 3),
            "access_count": row["access_count"],
        })
    nodes.sort(key=lambda node: (-node["accessibility"], node["record_id"]))
    relations = [
        {"from": row["from_record_id"], "type": row["relation_type"], "to": row["to_record_id"]}
        for row in memory.store.active_relation_rows()
        if not row["to_record_id"].startswith("anchor:")
    ]
    core = memory.store.current_view("core")
    return {
        "profile": memory.profile.name,
        "agent": memory.profile.agent,
        "owner": memory.profile.owner,
        "status": memory.status(),
        "core": {
            "title": core[0]["title"],
            "summary": core[0]["summary"],
            "detail": json.loads(core[0]["content"]),
            "revisions": len(memory.store.historical_view("core")),
        } if core else None,
        "timeline": memory.timeline(200),
        "nodes": nodes,
        "relations": relations,
        "open_core_proposals": memory.open_core_proposals(),
        "open_discussions": memory.open_discussions(),
        "open_loops": memory.open_loops(),
    }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Identity memory</title>
<style>
:root{--bg:#f7f5f1;--fg:#1d1c1a;--muted:#6b665e;--line:#e2ddd4;--card:#fff;--accent:#2f7d78;--warn:#b5562e;--dim:#9a948a}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--muted:#a19d95;--line:#2b2a27;--card:#1c1c1a;--accent:#6cc4bd;--warn:#e08a63;--dim:#6f6b64}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0}h2{font-size:15px;margin:28px 0 10px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:10px}
.muted{color:var(--muted)}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:baseline}
.pill{font-size:12px;border:1px solid var(--line);border-radius:99px;padding:1px 8px;color:var(--muted)}
.pill.pinned{color:var(--accent);border-color:var(--accent)}.pill.dormant{color:var(--dim)}.pill.fading{color:var(--warn)}
.alert{border-color:var(--warn)}.bar{height:4px;background:var(--line);border-radius:2px;margin-top:8px}.bar i{display:block;height:4px;background:var(--accent);border-radius:2px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:8px 0 0}dt{color:var(--muted)}dd{margin:0}
input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);font:inherit}
.tabs{display:flex;gap:6px;flex-wrap:wrap}.tabs button{font:inherit;padding:4px 12px;border-radius:99px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
.tabs button[aria-pressed=true]{border-color:var(--accent);color:var(--accent)}
ul.rel{margin:6px 0 0;padding-left:18px}pre{white-space:pre-wrap;font-size:13px}
</style></head><body><main>
<div class="row"><h1 id="agent">…</h1><span class="muted" id="meta"></span></div>
<div id="alerts"></div>
<h2>Core</h2><div id="core" class="card"></div>
<h2>Recall (read-only)</h2><input id="cue" placeholder="Type a cue, e.g. who are you" autocomplete="off"><div id="recall"></div>
<h2>Memory</h2><div class="tabs" id="filters"></div><div id="nodes"></div>
<h2>Timeline</h2><div id="timeline"></div>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let data,filter="all";
async function load(){data=await (await fetch("api/snapshot")).json();render()}
function render(){
  $("agent").textContent=data.agent;
  const s=data.status;
  $("meta").textContent=`profile ${data.profile} · owner ${data.owner} · `+Object.entries(s.activation||{}).map(([k,v])=>`${v} ${k}`).join(" · ");
  $("alerts").innerHTML=data.open_core_proposals.map(d=>`<div class="card alert"><b>Core proposal awaiting ${esc(data.owner)}</b><div class="muted">${esc(d.reason)} · ${esc(d.proposal_id)}${d.stale?" · stale":""}</div></div>`).join("")
    +data.open_discussions.map(d=>`<div class="card alert"><b>Legacy core discussion</b><div class="muted">${esc(d.reason)} · since ${esc(d.since)}</div></div>`).join("")
    +(data.open_loops.length?`<div class="card"><b>Open loops</b><ul class="rel">${data.open_loops.map(l=>`<li>${esc(l.title)}</li>`).join("")}</ul></div>`:"");
  const c=data.core;
  $("core").innerHTML=c?`<b>${esc(c.title)}</b> <span class="pill pinned">revision ${c.revisions}</span><div>${esc(c.summary)}</div>
    <dl>${Object.entries(c.detail.vho_stack).map(([k,v])=>`<dt>${esc(k.replaceAll("_"," "))}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    <dl><dt>recognized by</dt><dd>${c.detail.recognition_signature.map(esc).join("<br>")}</dd><dt>falsifier</dt><dd>${esc(c.detail.falsifier)}</dd></dl>`:"No core.";
  const states=["all","pinned","active","fading","dormant"];
  $("filters").innerHTML=states.map(x=>`<button aria-pressed="${x===filter}" data-f="${x}">${x}</button>`).join("");
  const rel={};data.relations.forEach(r=>(rel[r.from]??=[]).push(r));
  $("nodes").innerHTML=data.nodes.filter(n=>filter==="all"||n.state===filter).map(n=>`<div class="card"><div class="row"><b>${esc(n.title)}</b><span class="pill ${n.state}">${n.state}</span><span class="pill">${esc(n.domain)}</span><span class="muted">${esc(n.record_id)} · r${n.revision} · recalled ${n.access_count}×</span></div>
    <div>${esc(n.summary)}</div>${(rel[n.record_id]||[]).length?`<ul class="rel">${rel[n.record_id].map(r=>`<li class="muted">${esc(r.type)} → ${esc(r.to)}</li>`).join("")}</ul>`:""}
    <div class="bar"><i style="width:${Math.round(n.accessibility*100)}%"></i></div></div>`).join("")||`<p class="muted">Nothing here.</p>`;
  $("timeline").innerHTML=data.timeline.map(t=>`<div class="card"><div class="row"><b>${esc(t.title)}</b><span class="pill ${t.state}">${t.state}</span><span class="muted">${esc(t.at)}</span></div><div>${esc(t.summary)}</div></div>`).join("")||`<p class="muted">No phases yet.</p>`;
}
$("filters").addEventListener("click",e=>{if(e.target.dataset.f){filter=e.target.dataset.f;render()}});
let timer;$("cue").addEventListener("input",e=>{clearTimeout(timer);timer=setTimeout(async()=>{
  const q=e.target.value.trim();if(!q){$("recall").innerHTML="";return}
  const r=await (await fetch("api/retrieve?cue="+encodeURIComponent(q))).json();
  $("recall").innerHTML=(r.items||[]).map(i=>`<div class="card"><div class="row"><b>${esc(i.title)}</b><span class="pill ${i.state}">${i.state}</span><span class="pill">${esc(i.domain)}</span>${i.self_authored?'<span class="pill">self-authored</span>':''}</div><div>${esc(i.summary)}</div>${(i.work||[]).map(w=>`<div class="muted">work → ${esc(w.topic||w.ref)}${w.status?` · ${esc(w.status)} · next: ${esc(w.next_action||"—")}`:" · not found"}</div>`).join("")}<div class="muted" style="font-size:12px">${(i.reasons||[]).map(esc).join(" · ")}</div></div>`).join("")||`<p class="muted">Nothing recalled.</p>`;
  },250)});
load();
</script></body></html>
"""


def make_handler(memory: IdentityMemory):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value: Any) -> None:
            self._send(HTTPStatus.OK, json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    self._send(HTTPStatus.OK, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif url.path == "/api/snapshot":
                    self._json(snapshot(memory))
                elif url.path == "/api/retrieve":
                    cue = (parse_qs(url.query).get("cue") or [""])[0].strip()[:2000]
                    if not cue:
                        self._json({"packet": ""})
                    else:
                        result = memory.retrieve(cue, track=False)
                        self._json({"packet": result["packet"], "items": result["items"]})
                else:
                    self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            except Exception as error:  # shown to the local viewer only
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(error).encode("utf-8"), "text/plain")

        def log_message(self, *args):
            pass

    return Handler


def serve(memory: IdentityMemory, *, host: str = "127.0.0.1", port: int = 8767, open_browser: bool = True):
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the memory view only binds to localhost")
    server = ThreadingHTTPServer((host, port), make_handler(memory))
    url = f"http://{host}:{server.server_port}/"
    print(f"Identity memory view for {memory.profile.agent}: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
