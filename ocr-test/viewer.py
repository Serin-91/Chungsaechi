# PDF를 올리면 페이지 이미지 위에 인식 박스(줄 / 좌표로 묶은 덩어리)를 겹쳐 보여주는 로컬 뷰어.
#   python viewer.py → http://localhost:8765  (폴더 안 PDF 바로 열기: /?file=poster.pdf)
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ocr import parse

HERE = Path(__file__).parent
LOCK = threading.Lock()  # OCR 모델은 스레드 안전하지 않음 → 한 번에 하나씩

PAGE = r"""<!doctype html><meta charset="utf-8"><title>PDF OCR 뷰어</title>
<style>
:root{color-scheme:light dark;--bg:#f6f7f9;--fg:#1d2330;--mute:#6b7280;--card:#fff;--line:#e3e6eb;--ok:#16a34a;--mid:#d97706;--bad:#dc2626;--hl:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--fg:#e6e8eb;--mute:#9aa3ae;--card:#1c2027;--line:#2c323b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,"Malgun Gothic",sans-serif}
header{padding:16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0 8px 0 0}#status{color:var(--mute)}
label{display:flex;gap:6px;align-items:center;color:var(--mute)}
.page{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:16px;padding:16px}
@media (max-width:800px){.page{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;min-width:0}
.card h2{font-size:14px;margin:0 0 8px;display:flex;justify-content:space-between}
.img{position:relative}.img img{width:100%;display:block}.img svg{position:absolute;inset:0;width:100%;height:100%}
rect.blk{fill:transparent;stroke:var(--hl);stroke-width:2px;stroke-dasharray:6 3;vector-effect:non-scaling-stroke;cursor:pointer}
rect.blk.tbl{stroke:var(--ok);stroke-dasharray:none}
rect.blk.on{fill:rgba(37,99,235,.2);stroke-width:3px}polygon.dim{stroke-opacity:.35}
li .t small{display:block;color:var(--mute)}
li .t{min-width:0}li .t table{display:block;overflow-x:auto;max-width:100%;border-collapse:collapse;font-size:12px;margin-top:2px}li .t td{border:1px solid var(--line);padding:2px 5px;vertical-align:top;word-break:keep-all;min-width:3em}li .t td:empty{background:repeating-linear-gradient(45deg,transparent 0 4px,var(--line) 4px 5px)}
polygon{fill:transparent;stroke-width:2px;vector-effect:non-scaling-stroke;cursor:pointer}polygon.on{fill:rgba(37,99,235,.25);stroke:var(--hl)!important;stroke-width:3px}
ol{margin:0;padding-left:0;list-style:none;max-height:80vh;overflow:auto}
li{display:flex;gap:8px;padding:4px 6px;border-radius:4px;cursor:pointer}li.on{background:rgba(37,99,235,.15)}
li .s{font-variant-numeric:tabular-nums;width:3em;flex:none}li .t{word-break:break-all}
pre{white-space:pre-wrap;word-break:break-all;margin:8px 0 0;font:inherit;color:var(--mute)}
</style>
<header><h1>PDF OCR 뷰어</h1><input type="file" id="f" accept="application/pdf">
<label><select id="mode"><option value="blocks">묶음 보기</option><option value="lines">줄 보기 (원본)</option></select></label>
<label>최소 신뢰도 <input type="range" id="min" min="0" max="1" step="0.05" value="0"><span id="minv">0.00</span></label>
<span id="status">PDF를 선택하세요</span></header><main id="out"></main>
<script>
const $=s=>document.querySelector(s), color=s=>s>=.9?'var(--ok)':s>=.6?'var(--mid)':'var(--bad)';
let pages=[];
async function run(name,req){$('#status').textContent='처리 중… (스캔본은 페이지당 수십 초)'; $('#out').innerHTML=''; const t=performance.now();
  const r=await req; if(!r.ok){$('#status').textContent='실패: '+await r.text();return}
  pages=await r.json(); $('#status').textContent=`${name} · ${pages.length}쪽 · ${((performance.now()-t)/1000).toFixed(1)}초`; render()}
$('#f').onchange=e=>{const file=e.target.files[0]; if(file)run(file.name,fetch('/parse',{method:'POST',body:file}))};
const q=new URLSearchParams(location.search).get('file'); if(q)run(q,fetch('/parse?file='+encodeURIComponent(q)));
$('#min').oninput=e=>{$('#minv').textContent=(+e.target.value).toFixed(2); render()};
$('#mode').onchange=render;
function render(){const min=+$('#min').value, blk=$('#mode').value==='blocks';
  $('#out').innerHTML=pages.map((p,pi)=>{const ls=p.lines.map((l,i)=>({...l,i})).filter(l=>l.score>=min);
    const bs=p.blocks.map((b,i)=>({...b,i})).filter(b=>b.score>=min);
    const polys=ls.map(l=>`<polygon class="${blk?'dim':''}" ${blk?'':`data-k="${pi}-${l.i}"`} points="${l.box.map(q=>q.join(',')).join(' ')}" style="stroke:${color(l.score)}"><title>${esc(l.text)} (${l.score.toFixed(2)})</title></polygon>`).join('');
    const rects=blk?bs.map(b=>{const [x0,y0,x1,y1]=b.rect,pad=6;return `<rect class="blk${b.table?' tbl':''}" data-k="${pi}-b${b.i}" x="${x0-pad}" y="${y0-pad}" width="${x1-x0+2*pad}" height="${y1-y0+2*pad}" rx="8"><title>${esc(b.rows.join(' / '))}</title></rect>`}).join(''):'';
    const items=blk
      ?bs.map(b=>`<li data-k="${pi}-b${b.i}"><span class="s" style="color:${color(b.score)}">${b.score.toFixed(2)}</span><span class="t">${b.table?`<b>표 ${b.table.length}×${b.table[0].length}</b><table>${b.table.map(r=>`<tr>${r.map(c=>`<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</table>`:b.rows.length>1?b.rows.map((r,j)=>j?`<small>${esc(r)}</small>`:esc(r)).join(''):esc(b.text)||'<i>(빈 결과)</i>'}</span></li>`).join('')
      :ls.map(l=>`<li data-k="${pi}-${l.i}"><span class="s" style="color:${color(l.score)}">${l.score.toFixed(2)}</span><span class="t">${esc(l.text)||'<i>(빈 결과)</i>'}</span></li>`).join('');
    const n=blk?`묶음 ${bs.length}개 (원본 ${ls.length}줄)`:`인식 결과 ${ls.length}줄`, legend=blk?'파란 점선 = 묶음 · 초록 실선 = 표 · 빗금 = 빈 셀(병합)':'초록 ≥0.9 · 주황 ≥0.6 · 빨강 &lt;0.6';
    return `<section class="page"><div class="card"><h2><span>${pi+1}쪽</span><span>${p.method==='ocr'?'PaddleOCR (이미지)':'PyMuPDF (텍스트 레이어)'}</span></h2>${p.error?`<p style="color:var(--bad);margin:0 0 8px">⚠ ${esc(p.error)}</p>`:''}
    <div class="img"><img src="data:image/png;base64,${p.png}"><svg viewBox="0 0 ${p.w} ${p.h}">${polys}${rects}</svg></div></div>
    <div class="card"><h2><span>${n}</span><span style="color:var(--mute)">${legend}</span></h2>
    <ol>${items}</ol>
    <details><summary>전체 텍스트 복사용</summary><pre>${esc((blk?bs.map(b=>b.rows.join(' / ')):ls.map(l=>l.text)).join('\n'))}</pre></details></div></section>`}).join('')}
function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
document.addEventListener('mouseover',e=>{const k=e.target.closest('[data-k]')?.dataset.k;
  document.querySelectorAll('.on').forEach(x=>x.classList.remove('on'));
  if(k)document.querySelectorAll(`[data-k="${k}"]`).forEach(x=>x.classList.add('on'))});
document.addEventListener('click',e=>{const k=e.target.closest('li[data-k]')?.dataset.k;
  if(k)document.querySelector(`svg [data-k="${k}"]`)?.scrollIntoView({block:'center',behavior:'smooth'})});
</script>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        # /?file=x.pdf → 이 폴더의 PDF를 바로 열기 (/parse?file=x.pdf 로 가져감)
        url = urlparse(self.path)
        name = parse_qs(url.query).get("file", [""])[0]
        if url.path != "/parse":
            return self._send(200, "text/html; charset=utf-8", PAGE.encode())
        path = HERE / Path(name).name  # 폴더 밖 경로 차단
        if path.suffix.lower() != ".pdf" or not path.is_file():
            return self._send(404, "text/plain; charset=utf-8", f"없는 파일: {name}".encode())
        self._reply(path.read_bytes())

    def do_POST(self):
        self._reply(self.rfile.read(int(self.headers["Content-Length"])))

    def _reply(self, data):
        try:
            with LOCK:
                pages = parse(data)
        except Exception as e:  # 깨진 PDF 등은 화면에 이유를 보여줌
            return self._send(400, "text/plain; charset=utf-8", str(e).encode())
        for p in pages:
            p["png"] = base64.b64encode(p["png"]).decode()
        self._send(200, "application/json", json.dumps(pages, ensure_ascii=False).encode())

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print("http://localhost:8765")
    ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()
