"""Local web dashboard (served by the gateway) + memory list/search/delete API."""
from __future__ import annotations

import asyncio
import json
import urllib.request

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .security import make_guard

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>recallhook</title>
<style>
:root{--bg:#0f1218;--panel:#161b24;--line:#252c3a;--fg:#e8ebf2;--mut:#8a93a8;--acc:#43d9c1;--acc2:#6b9bff;--bad:#ff7b6b;--ok:#4cd98a}
@media(prefers-color-scheme:light){:root{--bg:#f4f6fa;--panel:#fff;--line:#dfe3ec;--fg:#1d2330;--mut:#6b7385;--acc:#0e9f8a;--acc2:#2f6fed;--bad:#c0392b;--ok:#17894f}}
*{box-sizing:border-box}body{font:14px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:20px;max-width:1100px;margin:auto}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}
h1{font-size:15px;letter-spacing:.12em;margin:0}h2{font-size:12px;letter-spacing:.1em;color:var(--mut);text-transform:uppercase;margin:0 0 10px;font-weight:600}
.pill{font-size:12px;border:1px solid var(--line);border-radius:99px;padding:2px 10px;color:var(--mut)}.pill i{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:6px}
.pill.bad i{background:var(--bad)}.mut{color:var(--mut)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));margin-bottom:12px}
.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.card .k{font-size:11px;letter-spacing:.08em;color:var(--mut);text-transform:uppercase}.card .v{font-size:28px;font-weight:600;color:var(--acc);margin-top:2px}.card .s{font-size:12px;color:var(--mut)}
.panel{margin-bottom:12px;overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mut);font-weight:500;font-size:11px;letter-spacing:.06em;text-transform:uppercase}
.badge{font-size:11px;border-radius:5px;padding:1px 7px;background:#2a3142;color:var(--fg)}.b-recall{background:rgba(107,155,255,.2);color:var(--acc2)}.b-stored{background:rgba(76,217,138,.18);color:var(--ok)}.b-recall_failed{background:rgba(255,123,107,.2);color:var(--bad)}
input{padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);width:min(440px,100%)}
button{border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:8px;padding:5px 10px;cursor:pointer}button:hover{border-color:var(--acc)}.chip{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:2px 12px;margin:0 8px 6px 0;font-size:13px;color:var(--mut)}.chip b{color:var(--fg)}
.del{color:var(--bad)}.banner{border:1px solid var(--bad);background:rgba(255,123,107,.12);border-radius:10px;padding:10px 14px;margin-bottom:12px;font-size:13px}.banner b{color:var(--bad)}
a{color:var(--acc2)}svg text{fill:var(--mut);font-size:10px}.legend span{margin-right:14px;font-size:12px;color:var(--mut)}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
</style>
<header><h1>RECALLHOOK</h1><span class=pill id=health><i></i><span>checking</span></span><span class=mut id=sub></span><span class=mut style=margin-left:auto id=upd></span></header>
<div id=banner></div>
<div class=grid id=cards></div>
<div class=panel><h2>Activity, last 14 days</h2><div class=legend><span><i style=background:var(--acc2)></i>recalls</span><span><i style=background:var(--ok)></i>memories created</span></div><div id=chart></div></div>
<div class=panel><h2>Why some prompts aren't stored <span class=mut>(last 30 days)</span></h2><div id=skipchips></div>
<p class=mut style=margin:6px 0 0>recallhook only stores statements of lasting facts, like "I always...", "my project X uses...", "Atlas deploys to...". Questions and task requests ("fix this bug") are recalled for but never stored. Start a prompt with <b>"Remember that ..."</b> to store it for sure.</p></div>
<div class=panel><h2>Live activity</h2><table><thead><tr><th>Time<th>Event<th>Source<th>Hits<th>ms<th>~Tokens<th>Detail</tr></thead><tbody id=feed></tbody></table></div>
<div class=panel><h2>Memories <span class=mut id=count></span></h2>
<p><input id=q placeholder="Search memories (shows relevance score)"> <button id=clr>clear</button></p>
<table><thead><tr><th>Memory<th style=width:70px>Score<th style=width:140px>Updated<th style=width:70px></tr></thead><tbody id=rows></tbody></table></div>
<div class="panel mut" id=hr style=font-size:12px></div>
<script>
const A='/_recallhook/api',$=s=>document.querySelector(s),H={'X-HM-Client':'ui'};
const esc=t=>String(t==null?'':t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function j(u,o){const r=await fetch(u,o);if(!r.ok)throw new Error(r.status);return r.json()}
const card=(k,v,s)=>`<div class=card><div class=k>${esc(k)}</div><div class=v>${esc(v)}</div><div class=s>${esc(s||'')}</div></div>`;
const hhmm=ts=>{const d=new Date(ts*1000);return d.toLocaleDateString(undefined,{month:'short',day:'numeric'})+' '+d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})};
function chart(rows){const W=1040,Ht=140,n=14,bw=W/n,days=[];for(let i=n-1;i>=0;i--){const d=new Date(Date.now()-i*864e5);days.push(d.toLocaleDateString('en-CA'))}
 const m=Math.max(1,...rows.map(r=>Math.max(r.recall,r.stored)));let s=`<svg viewBox="0 0 ${W} ${Ht+18}" width=100% >`;
 days.forEach((d,i)=>{const r=rows.find(x=>x.day===d)||{recall:0,stored:0};const x=i*bw+bw*.15,w=bw*.32;
  const h1=r.recall/m*(Ht-8),h2=r.stored/m*(Ht-8);
  s+=`<rect x=${x} y=${Ht-h1} width=${w} height=${h1} fill="var(--acc2)" rx=2><title>${d}: ${r.recall} recalls</title></rect><rect x=${x+w+3} y=${Ht-h2} width=${w} height=${h2} fill="var(--ok)" rx=2><title>${d}: ${r.stored} memories created</title></rect><text x=${i*bw+bw/2} y=${Ht+13} text-anchor=middle>${d.slice(5)}</text>`});
 $('#chart').innerHTML=s+'</svg>'}
async function refresh(){try{
 const [h,s,a,se]=await Promise.all([j('/_recallhook/health'),j(A+'/summary'),j(A+'/activity?limit=25'),j(A+'/series?days=14')]);
 $('#health').className='pill';$('#health span').textContent='Healthy';$('#sub').textContent=`${h.llm} · ${h.embedder}`;$('#upd').textContent='Updated '+new Date().toLocaleTimeString();
 $('#cards').innerHTML=card('Memories',s.memories??'?','stored facts')+card('Recalls',s.recalls,s.recalls_today+' today')+card('Hit rate',s.hit_rate+'%','recalls that found something')+card('Recall latency',s.avg_ms+' ms','average')+card('Tokens injected','~'+s.tokens_injected.toLocaleString(),'estimated, all time')+card('Created',s.memories_created,'memories extracted')+card('Queue',s.capture_queue,'captures waiting')+card('Failures',s.failures,'recall timeouts/errors');
 $('#banner').innerHTML=(s.problems||[]).map(p=>`<div class=banner><b>Needs attention:</b> ${esc(p)}</div>`).join('');
 const sk=Object.entries(s.skipped||{});
 $('#skipchips').innerHTML=sk.length?sk.map(([r,n])=>`<span class=chip><b>${n}</b> ${esc(r)}</span>`).join(''):'<span class=mut>Nothing skipped yet.</span>';
 chart(se);
 $('#feed').innerHTML=a.map(e=>`<tr><td class=mut>${hhmm(e.ts)}<td><span class="badge b-${esc(e.kind)}">${esc(e.kind.replace('_',' '))}</span><td class=mut>${esc(e.source)}<td>${e.hits}<td>${e.ms}<td>${e.tokens||''}<td class=mut>${esc(e.detail)}</tr>`).join('')||'<tr><td colspan=7 class=mut>no activity yet</td></tr>';
 try{const x=await j(A+'/headroom');$('#hr').innerHTML=x.ok?`Headroom proxy is running (its own estimates: ${Number(x.tokens_removed).toLocaleString()} tokens removed, ${x.requests} requests) · <a href="${x.dashboard}" target=_blank>Headroom dashboard</a>`:'Headroom proxy is not running (optional).'}catch(e){}
}catch(e){$('#health').className='pill bad';$('#health span').textContent='Gateway error';}}
async function load(){const q=$('#q').value.trim();
 const d=q?await j(A+'/search?q='+encodeURIComponent(q)):await j(A+'/memories');
 $('#count').textContent=`(${d.length}${q?' matches':''})`;
 $('#rows').innerHTML=d.map(m=>`<tr><td>${esc(m.memory)}<td>${m.score==null?'':m.score}<td class=mut>${esc((m.updated_at||m.created_at||'').slice(0,16).replace('T',' '))}<td><button class=del data-id="${esc(m.id)}">delete</button></tr>`).join('')||'<tr><td colspan=4 class=mut>nothing here yet</td></tr>'}
$('#rows').onclick=async e=>{const id=e.target.dataset.id;if(id&&confirm('Delete this memory?')){await j(A+'/memories/'+id,{method:'DELETE',headers:H});load();refresh()}};
let t;$('#q').oninput=()=>{clearTimeout(t);t=setTimeout(load,300)};$('#clr').onclick=()=>{$('#q').value='';load()};
refresh();load();setInterval(refresh,4000);setInterval(()=>{if(!$('#q').value)load()},15000);
</script>"""


def build_router(cfg, memory) -> APIRouter:
    r = APIRouter(prefix="/_recallhook")
    guard = make_guard(cfg)

    @r.get("/ui", response_class=HTMLResponse)
    async def ui(request: Request):
        guard(request)
        return PAGE

    @r.get("/api/memories")
    async def memories(request: Request, user: str | None = None):
        guard(request)
        return await asyncio.to_thread(memory.list_all, user or cfg.user_id)

    @r.get("/api/search")
    async def search(request: Request, q: str, user: str | None = None):
        guard(request)
        return await asyncio.to_thread(memory.search_scored, q, user or cfg.user_id)

    @r.delete("/api/memories/{memory_id}")
    async def delete(request: Request, memory_id: str):
        guard(request, mutating=True)
        await asyncio.to_thread(memory.delete, memory_id)
        return {"deleted": memory_id}

    @r.get("/api/headroom")
    async def headroom(request: Request):
        guard(request)

        def fetch():
            with urllib.request.urlopen(cfg.upstream.rstrip("/") + "/stats", timeout=2) as resp:
                return json.loads(resp.read())

        dash = cfg.upstream.rstrip("/") + "/dashboard"
        try:
            summ = (await asyncio.to_thread(fetch)).get("summary", {})
            c = summ.get("compression", {})
            return {"ok": True, "dashboard": dash, "requests": summ.get("api_requests", 0),
                    "tokens_removed": c.get("total_tokens_removed", 0)}
        except Exception:  # noqa: BLE001
            return {"ok": False, "dashboard": dash}

    return r
