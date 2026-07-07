#!/usr/bin/env python3
"""Sentinel Print — a small print SUITE for the EPSON L1250 CUPS queue.

Features: a persistent file LIBRARY (upload once, print many), server-side
PREVIEW + thumbnails (pdftoppm/ghostscript), print OPTIONS (copies, pages-per-
sheet a.k.a. "double page", colour/mono, paper size, page range), a live PRINTER
STATUS, and a configurable AUTO-DELETE retention policy with a background sweeper.

Data lives under /data (a mounted volume) so the library survives container
restarts. Binds 0.0.0.0:6632; reached on localhost (cloudflared -> CF Access)."""
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, abort, request, send_file

app = Flask(__name__)
PRINTER = os.environ.get("PRINTER_NAME", "EPSON_L1250")
DATA = os.environ.get("DATA_DIR", "/data")
LIB = os.path.join(DATA, "library")
THUMBS = os.path.join(DATA, "thumbs")
CONFIG_PATH = os.path.join(DATA, "config.json")
MAX_MB = int(os.environ.get("MAX_MB", "50"))
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024

ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".txt"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff"}
SIZES = ["A4", "A5", "A6", "B5", "Letter", "Legal", "4x6", "Postcard"]
NUP = [1, 2, 4, 6, 9]
DEFAULT_CFG = {"auto_delete": False, "retention_hours": 168}  # 7 days default when on

os.makedirs(LIB, exist_ok=True)
os.makedirs(THUMBS, exist_ok=True)


# ── config ────────────────────────────────────────────────────────────────
def load_cfg():
    try:
        with open(CONFIG_PATH) as f:
            return {**DEFAULT_CFG, **json.load(f)}
    except Exception:
        return dict(DEFAULT_CFG)


def save_cfg(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f)
    os.replace(tmp, CONFIG_PATH)


# ── library helpers ─────────────────────────────────────────────────────────
_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def _sanitize(name):
    name = os.path.basename(name).strip().replace(" ", "_")
    name = _SAFE.sub("", name) or "file"
    return name[:120]


def _path_for(fid):
    """Return the stored path for an id, or None. id is the '<hex>' prefix."""
    if not re.fullmatch(r"[0-9a-f]{12}", fid or ""):
        return None
    for fn in os.listdir(LIB):
        if fn.startswith(fid + "__"):
            return os.path.join(LIB, fn)
    return None


def _meta(fn):
    fid, _, original = fn.partition("__")
    p = os.path.join(LIB, fn)
    st = os.stat(p)
    ext = os.path.splitext(original)[1].lower()
    return {
        "id": fid,
        "name": original,
        "ext": ext,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "kind": "image" if ext in IMG_EXT else ("pdf" if ext == ".pdf" else "file"),
    }


def list_library():
    items = []
    for fn in os.listdir(LIB):
        if "__" not in fn:
            continue
        try:
            items.append(_meta(fn))
        except OSError:
            pass
    items.sort(key=lambda m: m["mtime"], reverse=True)
    return items


def _human_size(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    return f"{n/1024/1024:.1f} MB"


def _ago(mtime):
    secs = max(0, time.time() - mtime)
    if secs < 3600:
        return f"{int(secs//60)}m ago"
    if secs < 86400:
        return f"{int(secs//3600)}h ago"
    return f"{int(secs//86400)}d ago"


# ── retention sweeper ───────────────────────────────────────────────────────
def _sweep_once():
    cfg = load_cfg()
    if not cfg.get("auto_delete"):
        return
    cutoff = time.time() - cfg.get("retention_hours", 168) * 3600
    for fn in list(os.listdir(LIB)):
        p = os.path.join(LIB, fn)
        try:
            if os.stat(p).st_mtime < cutoff:
                os.remove(p)
                _drop_thumb(fn.partition("__")[0])
        except OSError:
            pass


def _sweeper():
    while True:
        try:
            _sweep_once()
        except Exception:
            pass
        time.sleep(300)  # every 5 min


# ── thumbnails / preview ─────────────────────────────────────────────────────
def _thumb_path(fid):
    return os.path.join(THUMBS, f"{fid}.png")


def _drop_thumb(fid):
    try:
        os.remove(_thumb_path(fid))
    except OSError:
        pass


def _ensure_thumb(fid, src):
    tp = _thumb_path(fid)
    if os.path.exists(tp) and os.path.getmtime(tp) >= os.path.getmtime(src):
        return tp
    ext = os.path.splitext(src)[1].lower()
    try:
        if ext == ".pdf":
            # pdftoppm writes <prefix>-1.png (or -01); render page 1 at low res.
            prefix = os.path.join(THUMBS, f"{fid}_g")
            subprocess.run(["pdftoppm", "-png", "-f", "1", "-l", "1", "-scale-to", "300", src, prefix],
                           capture_output=True, timeout=25)
            cand = next((os.path.join(THUMBS, f) for f in os.listdir(THUMBS)
                         if f.startswith(f"{fid}_g")), None)
            if cand:
                os.replace(cand, tp)
                return tp
        elif ext in IMG_EXT:
            return src  # browser scales the image directly
    except Exception:
        pass
    return None


# ── printer status ──────────────────────────────────────────────────────────
def printer_status():
    try:
        r = subprocess.run(["lpstat", "-p", PRINTER], capture_output=True, text=True, timeout=8)
        out = (r.stdout or "").strip().lower()
        if "disabled" in out or "stopped" in out:
            return {"state": "stopped", "text": r.stdout.strip()}
        if "printing" in out or "processing" in out:
            return {"state": "printing", "text": r.stdout.strip()}
        if "idle" in out:
            return {"state": "idle", "text": r.stdout.strip()}
        return {"state": "unknown", "text": (r.stdout or r.stderr or "").strip()}
    except Exception as e:
        return {"state": "unknown", "text": str(e)}


# ── routes ──────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return {"ok": True, "printer": PRINTER}


@app.route("/api/state")
def api_state():
    return {
        "printer": PRINTER,
        "status": printer_status(),
        "config": load_cfg(),
        "sizes": SIZES,
        "nup": NUP,
        "files": [
            {**m, "size_h": _human_size(m["size"]), "ago": _ago(m["mtime"]),
             "when": datetime.fromtimestamp(m["mtime"], timezone.utc).strftime("%Y-%m-%d %H:%M")}
            for m in list_library()
        ],
    }


@app.route("/api/upload", methods=["POST"])
def api_upload():
    files = request.files.getlist("file")
    saved, errs = 0, []
    for f in files:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED:
            errs.append(f'{f.filename}: unsupported type "{ext}"')
            continue
        fid = uuid.uuid4().hex[:12]
        f.save(os.path.join(LIB, f"{fid}__{_sanitize(f.filename)}"))
        saved += 1
    return {"ok": not errs, "saved": saved, "errors": errs}


@app.route("/thumb/<fid>")
def thumb(fid):
    p = _path_for(fid)
    if not p:
        abort(404)
    t = _ensure_thumb(fid, p)
    if not t:
        abort(404)
    return send_file(t, mimetype="image/png" if t.endswith(".png") else None, max_age=0)


@app.route("/preview/<fid>")
def preview(fid):
    p = _path_for(fid)
    if not p:
        abort(404)
    ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
    return send_file(p, mimetype=ctype, as_attachment=False,
                     download_name=os.path.basename(p).partition("__")[2])


@app.route("/api/delete/<fid>", methods=["POST"])
def api_delete(fid):
    p = _path_for(fid)
    if not p:
        return {"ok": False, "error": "not found"}, 404
    try:
        os.remove(p)
        _drop_thumb(fid)
        return {"ok": True}
    except OSError as e:
        return {"ok": False, "error": str(e)}, 500


@app.route("/api/settings", methods=["POST"])
def api_settings():
    body = request.get_json(silent=True) or {}
    cfg = load_cfg()
    cfg["auto_delete"] = bool(body.get("auto_delete", cfg["auto_delete"]))
    try:
        h = int(body.get("retention_hours", cfg["retention_hours"]))
        cfg["retention_hours"] = max(1, min(24 * 365, h))
    except (TypeError, ValueError):
        pass
    save_cfg(cfg)
    return {"ok": True, "config": cfg}


@app.route("/api/print/<fid>", methods=["POST"])
def api_print(fid):
    p = _path_for(fid)
    if not p:
        return {"ok": False, "error": "not found"}, 404
    body = request.get_json(silent=True) or {}
    cmd = ["lp", "-d", PRINTER]
    try:
        copies = max(1, min(50, int(body.get("copies", 1))))
    except (TypeError, ValueError):
        copies = 1
    cmd += ["-n", str(copies)]
    try:
        nup = int(body.get("number_up", 1))
    except (TypeError, ValueError):
        nup = 1
    if nup in NUP and nup != 1:
        cmd += ["-o", f"number-up={nup}"]
    cmd += ["-o", f"print-color-mode={'monochrome' if body.get('mono') else 'color'}"]
    size = body.get("size")
    if size in SIZES:
        cmd += ["-o", f"media={size}"]
    pr = (body.get("page_ranges") or "").strip()
    if re.fullmatch(r"[0-9,\- ]+", pr) and pr:
        cmd += ["-o", f"page-ranges={pr.replace(' ', '')}"]
    cmd.append(p)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "print timed out"}, 500
    if r.returncode == 0:
        return {"ok": True, "job": (r.stdout or "queued").strip()}
    return {"ok": False, "error": (r.stderr or r.stdout or "unknown").strip()[:300]}, 500


@app.route("/")
def index():
    return PAGE


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Sentinel Print</title>
<style>
 :root{color-scheme:dark}
 *{box-sizing:border-box}
 body{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:20px}
 .wrap{max-width:900px;margin:0 auto}
 header{display:flex;align-items:center;gap:12px;margin-bottom:6px}
 h1{font-size:22px;margin:0}
 .dot{width:10px;height:10px;border-radius:50%;background:#8b949e;display:inline-block}
 .dot.idle{background:#2ea043}.dot.printing{background:#d29922;animation:pulse 1s infinite}.dot.stopped{background:#f85149}
 @keyframes pulse{50%{opacity:.4}}
 .sub{color:#8b949e;font-size:13px;margin:0 0 18px}
 .card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:16px;margin-bottom:16px}
 .drop{border:1.5px dashed #30363d;border-radius:10px;padding:22px;text-align:center;color:#8b949e;cursor:pointer}
 .drop.drag{border-color:#2ea043;background:#0f2417}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
 .file{background:#0d1117;border:1px solid #30363d;border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
 .thumb{height:110px;background:#010409;display:flex;align-items:center;justify-content:center;overflow:hidden}
 .thumb img{max-width:100%;max-height:100%;object-fit:contain}
 .thumb .ico{font-size:38px}
 .fmeta{padding:8px 10px;font-size:12px}
 .fname{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .fsub{color:#6e7681;font-size:11px;margin-top:2px}
 .facts{display:flex;border-top:1px solid #30363d}
 .facts button{flex:1;background:none;border:0;color:#58a6ff;padding:7px 0;font-size:12px;cursor:pointer;border-right:1px solid #30363d}
 .facts button:last-child{border-right:0}.facts button:hover{background:#161b22}
 .facts button.del{color:#f85149}
 button.primary{background:#238636;color:#fff;border:0;border-radius:8px;padding:10px 16px;font-size:14px;cursor:pointer}
 button.primary:hover{background:#2ea043}
 label{font-size:13px;color:#8b949e;display:block;margin:10px 0 4px}
 select,input[type=text],input[type=number]{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:7px;width:100%}
 .row{display:flex;gap:12px}.row>div{flex:1}
 .toggle{display:flex;align-items:center;gap:8px;margin:6px 0}
 .msg{padding:9px 12px;border-radius:8px;margin-bottom:12px;font-size:13px}
 .ok{background:#10301a;border:1px solid #238636;color:#7ee787}.err{background:#3a1416;border:1px solid #b62324;color:#ffa198}
 .modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;padding:16px;z-index:9}
 .modal.on{display:flex}
 .sheet{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px;max-width:420px;width:100%}
 .sheet h3{margin:0 0 2px}.muted{color:#8b949e;font-size:12px}
 .sheet .actions{display:flex;gap:10px;margin-top:16px}
 .ghost{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:10px 16px;cursor:pointer}
 .empty{color:#6e7681;text-align:center;padding:24px}
 .settings{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end}
 .settings>div{flex:1;min-width:130px}
 h2{font-size:14px;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin:0 0 12px}
</style></head><body><div class=wrap>
 <header><h1>&#128424;&#65039; Sentinel Print</h1><span id=dot class=dot></span><span id=pstat class=sub style=margin:0></span></header>
 <p class=sub>EPSON L1250 &middot; full library + print suite</p>
 <div id=msg></div>

 <div class=card>
  <div id=drop class=drop>Drop files here or <b>click to choose</b><br><span class=muted>PDF, image, or text &middot; max __MAX__MB</span>
   <input id=file type=file multiple accept=".pdf,.png,.jpg,.jpeg,.gif,.bmp,.tiff,.txt" style=display:none></div>
 </div>

 <div class=card>
  <h2>Library</h2>
  <div id=lib class=grid></div>
 </div>

 <div class=card>
  <h2>Auto-delete</h2>
  <div class=settings>
   <div class=toggle style=flex:0><input type=checkbox id=ad><label for=ad style=margin:0>Enabled</label></div>
   <div><label>Delete files older than (hours)</label><input type=number id=rh min=1 value=168></div>
   <div style=flex:0><button class=primary id=savecfg>Save</button></div>
  </div>
  <p class=muted id=cfghint style=margin:10px_0_0></p>
 </div>
</div>

<div id=modal class=modal><div class=sheet>
 <h3 id=mtitle>Print</h3><p class=muted id=mname></p>
 <div class=row><div><label>Copies</label><input type=number id=copies value=1 min=1 max=50></div>
  <div><label>Pages / sheet (double-page)</label><select id=nup></select></div></div>
 <div class=row><div><label>Paper size</label><select id=size></select></div>
  <div><label>Colour</label><select id=color><option value=color>Colour</option><option value=mono>Grayscale</option></select></div></div>
 <label>Page range (e.g. 1-3,5) &mdash; blank = all</label><input type=text id=range placeholder=all>
 <div class=actions><button class=primary id=doprint style=flex:1>Print</button><button class=ghost id=mcancel>Cancel</button></div>
</div></div>

<script>
const $=s=>document.querySelector(s), lib=$('#lib');
let CUR=null, SIZES=[], NUP=[];
function msg(t,ok){$('#msg').innerHTML=`<div class="msg ${ok?'ok':'err'}">${t}</div>`;if(ok)setTimeout(()=>$('#msg').innerHTML='',4000);}
function ico(k){return k=='pdf'?'📄':k=='image'?'🖼️':'📝';}
async function refresh(){
 const s=await (await fetch('/api/state')).json();
 SIZES=s.sizes;NUP=s.nup;
 $('#dot').className='dot '+s.status.state;$('#pstat').textContent=s.status.state;
 $('#ad').checked=s.config.auto_delete;$('#rh').value=s.config.retention_hours;
 $('#cfghint').textContent=s.config.auto_delete?`On — files auto-removed after ${s.config.retention_hours}h.`:'Off — files kept until you delete them.';
 if(!s.files.length){lib.innerHTML='<div class=empty>No files yet. Upload something above.</div>';return;}
 lib.innerHTML=s.files.map(f=>`<div class=file>
   <div class=thumb>${f.kind=='file'?`<span class=ico>${ico(f.kind)}</span>`:`<img loading=lazy src="/thumb/${f.id}" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'ico',textContent:'${ico(f.kind)}'}))">`}</div>
   <div class=fmeta><div class=fname title="${f.name}">${f.name}</div><div class=fsub>${f.size_h} &middot; ${f.ago}</div></div>
   <div class=facts><button onclick="window.open('/preview/${f.id}','_blank')">Preview</button>
    <button onclick='openPrint(${JSON.stringify(f)})'>Print</button>
    <button class=del onclick="del('${f.id}','${f.name.replace(/'/g,"")}')">Delete</button></div></div>`).join('');
}
function openPrint(f){CUR=f;$('#mname').textContent=f.name;
 $('#nup').innerHTML=NUP.map(n=>`<option value=${n}>${n==1?'1 (normal)':n+' per sheet'}</option>`).join('');
 $('#size').innerHTML=SIZES.map(s=>`<option ${s=='A4'?'selected':''}>${s}</option>`).join('');
 $('#copies').value=1;$('#range').value='';$('#color').value='color';
 $('#modal').classList.add('on');}
$('#mcancel').onclick=()=>$('#modal').classList.remove('on');
$('#doprint').onclick=async()=>{
 const body={copies:+$('#copies').value,number_up:+$('#nup').value,size:$('#size').value,
   mono:$('#color').value=='mono',page_ranges:$('#range').value};
 const r=await (await fetch('/api/print/'+CUR.id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
 $('#modal').classList.remove('on');
 msg(r.ok?`Sent to printer &#9989; <small>${r.job||''}</small>`:`Print failed: ${r.error}`,r.ok);
};
async function del(id,name){if(!confirm('Delete "'+name+'"?'))return;
 const r=await (await fetch('/api/delete/'+id,{method:'POST'})).json();
 if(r.ok){refresh();}else{msg('Delete failed: '+(r.error||''),false);}}
// upload
const drop=$('#drop'),fin=$('#file');
drop.onclick=()=>fin.click();
drop.ondragover=e=>{e.preventDefault();drop.classList.add('drag');};
drop.ondragleave=()=>drop.classList.remove('drag');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('drag');up(e.dataTransfer.files);};
fin.onchange=()=>up(fin.files);
async function up(files){if(!files.length)return;const fd=new FormData();
 for(const f of files)fd.append('file',f);
 const r=await (await fetch('/api/upload',{method:'POST',body:fd})).json();
 fin.value='';
 msg(r.saved?`Added ${r.saved} file(s)${r.errors.length?'; '+r.errors.join('; '):''}`:('Upload failed: '+(r.errors.join('; ')||'')),r.saved>0);
 refresh();}
$('#savecfg').onclick=async()=>{
 const r=await (await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({auto_delete:$('#ad').checked,retention_hours:+$('#rh').value})})).json();
 msg('Settings saved',true);refresh();};
refresh();setInterval(refresh,15000);
</script></body></html>""".replace("__MAX__", str(MAX_MB))


threading.Thread(target=_sweeper, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=6632)
