"""
app.py - FaceGotcha: find the photos you're in. Runs ONLY on this laptop.

    python app.py        then open  http://127.0.0.1:5000

Uploads are COPIED into the 'workspace' folder next to this file:
    workspace/refs      your reference photos (max 5)
    workspace/photos    the photos to search
    workspace/results   matched / maybe / rejected + results.csv
Your original photos are never touched.
"""

import os
import shutil
import tempfile
import threading
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

import find_me

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)  # find_me.py looks for models/ relative to the current folder

WORK = ROOT / "workspace"
REFS, PHOTOS, RESULTS = WORK / "refs", WORK / "photos", WORK / "results"

# Settings found by testing on a small set of photos (see the write-up).
THRESHOLD = 0.53      # at or above: auto-matched
REVIEW_FLOOR = 0.38   # between this and THRESHOLD: you decide by swiping
MAX_REFS = 5

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 ** 3  # 2 GB per request

job = {"state": "idle", "done": 0, "total": 0, "error": None, "counts": None}
undo_stack = []
_tools = None


def get_tools():
    global _tools
    if _tools is None:
        _tools = find_me.FaceTools()   # loads the two models once
    return _tools


def images_in(folder: Path):
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in find_me.IMAGE_EXTENSIONS)


def save_uploads(files, dest: Path):
    """Save uploaded images into dest. Only the file's own name is used (no folders)."""
    dest.mkdir(parents=True, exist_ok=True)
    saved = 0
    for f in files:
        name = Path(f.filename or "").name
        if Path(name).suffix.lower() not in find_me.IMAGE_EXTENSIONS:
            continue
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        target = dest / name
        n = 1
        duplicate = False
        while target.exists():
            if target.stat().st_size == size:      # same name AND same size: already added
                duplicate = True
                break
            target = dest / f"{Path(name).stem}_{n}{Path(name).suffix}"   # different file, same name
            n += 1
        if duplicate:
            continue
        f.save(target)
        saved += 1
    return saved


# ----------------------------------------------------------------- pages / state
@app.get("/")
def home():
    return PAGE


@app.get("/api/state")
def state():
    pending = images_in(RESULTS / "maybe")
    return jsonify(
        refs=[p.name for p in images_in(REFS)],
        photos=len(images_in(PHOTOS)),
        job=job,
        maybe=len(pending),
        matched=len(images_in(RESULTS / "matched")),
        rejected=len(images_in(RESULTS / "rejected")),
        next=(pending[-1].name if pending else None),
    )


@app.get("/img/<folder>/<path:name>")
def img(folder, name):
    if folder not in ("refs", "maybe", "matched"):
        return "Not found", 404
    base = REFS if folder == "refs" else RESULTS / folder
    return send_from_directory(base.resolve(), name)   # refuses paths that escape the folder


# ----------------------------------------------------------------- uploads
@app.post("/api/upload/refs")
def upload_refs():
    files = request.files.getlist("files")
    if not 1 <= len(files) <= MAX_REFS:
        return jsonify(ok=False, error=f"Choose 1 to {MAX_REFS} photos."), 400
    shutil.rmtree(REFS, ignore_errors=True)        # choosing again replaces the old set
    save_uploads(files, REFS)
    return jsonify(ok=True)


@app.post("/api/upload/photos")
def upload_photos():
    save_uploads(request.files.getlist("files"), PHOTOS)
    return jsonify(ok=True)


# ----------------------------------------------------------------- search job
def worker():
    try:
        shutil.rmtree(RESULTS, ignore_errors=True)
        undo_stack.clear()

        def progress(done, total):
            job.update(done=done, total=total)

        counts = find_me.run_search(REFS, PHOTOS, RESULTS, THRESHOLD, REVIEW_FLOOR,
                                    progress=progress, tools=get_tools())
        job.update(state="done", counts=counts)
    except Exception as e:                          # show the problem in the page
        job.update(state="error", error=str(e))


@app.post("/api/run")
def run():
    if job["state"] == "running":
        return jsonify(ok=False, error="Already running."), 409
    if not images_in(REFS) or not images_in(PHOTOS):
        return jsonify(ok=False, error="Add reference photos and photos to search first."), 400
    job.update(state="running", done=0, total=len(images_in(PHOTOS)), error=None, counts=None)
    threading.Thread(target=worker, daemon=True).start()
    return jsonify(ok=True)


# ----------------------------------------------------------------- review
@app.post("/api/decide")
def decide():
    data = request.get_json(force=True)
    name = Path(data.get("name", "")).name
    src = RESULTS / "maybe" / name
    if not src.is_file():
        return jsonify(ok=False, error="File not found"), 404
    dest_dir = RESULTS / ("matched" if data.get("decision") == "me" else "rejected")
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest_dir / name))
    undo_stack.append((dest_dir / name, src))
    return jsonify(ok=True)


@app.post("/api/undo")
def undo():
    if not undo_stack:
        return jsonify(ok=False)
    moved_to, original = undo_stack.pop()
    if moved_to.is_file():
        shutil.move(str(moved_to), str(original))
    return jsonify(ok=True)


@app.get("/api/download")
def download():
    matched = RESULTS / "matched"
    if not images_in(matched):
        return "Nothing to download yet.", 404
    base = Path(tempfile.mkdtemp()) / "my_photos"
    zip_path = shutil.make_archive(str(base), "zip", matched)
    return send_file(zip_path, as_attachment=True, download_name="my_photos.zip")


@app.post("/api/reset")
def reset():
    if job["state"] == "running":
        return jsonify(ok=False, error="Wait for the search to finish."), 409
    shutil.rmtree(WORK, ignore_errors=True)         # only our copies; originals are elsewhere
    undo_stack.clear()
    job.update(state="idle", done=0, total=0, error=None, counts=None)
    return jsonify(ok=True)


# ----------------------------------------------------------------- the page
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FaceGotcha</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<style>
  :root { --bg:#F6F3FF; --ink:#1A1530; --muted:#5E5877; --grape:#6D4AFF; --mint:#20D6A0; --lemon:#FFD93D;
          --pink:#FF5C9D; --sky:#5CC8FF; }
  * { box-sizing:border-box; }
  body { margin:0; color:var(--ink); font-family:"Segoe UI", system-ui, -apple-system, sans-serif; line-height:1.45;
         background-color:var(--bg); background-image:radial-gradient(#D9D1F5 1.5px, transparent 1.5px); background-size:22px 22px; }
  #fx { position:fixed; inset:0; pointer-events:none; z-index:50; }
  .wrap { max-width:780px; margin:0 auto; padding:28px 16px 72px; }
  header { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:22px; }
  h1 { font-family:"Segoe UI Black","Arial Black",system-ui,sans-serif; font-weight:900; font-size:2.3rem; margin:0; letter-spacing:-.02em; display:flex; align-items:center; gap:10px; }
  .logo { width:62px; height:62px; display:block; }
  .wiggle { display:inline-block; animation:wig 2.4s ease-in-out infinite; }
  @keyframes wig { 0%,100% { rotate:-8deg; } 50% { rotate:10deg; } }
  .sub { color:var(--muted); margin:4px 0 0; max-width:30em; }
  .hud { flex:none; width:92px; height:92px; border-radius:50%; background:var(--lemon); border:3px solid var(--ink);
         box-shadow:4px 4px 0 var(--ink); display:flex; flex-direction:column; align-items:center; justify-content:center; rotate:6deg; }
  .hudnum { font-family:"Segoe UI Black","Arial Black",sans-serif; font-size:2rem; line-height:1; }
  .hudlbl { font-weight:700; font-size:.8rem; }
  section { background:#fff; border:3px solid var(--ink); border-radius:16px; padding:18px; margin-bottom:20px; box-shadow:6px 6px 0 var(--ink); }
  h2 { display:flex; align-items:center; gap:10px; font-size:1.2rem; margin:0 0 4px; font-weight:800; }
  .badge { width:34px; height:34px; border-radius:50%; border:3px solid var(--ink); display:grid; place-items:center; font-weight:900; flex:none; }
  .b1 { background:var(--pink); } .b2 { background:var(--sky); } .b3 { background:var(--lemon); }
  .hint { color:var(--muted); font-size:.95rem; margin:0 0 12px; }
  .btn, button { font:inherit; font-weight:800; padding:11px 18px; border-radius:12px; border:3px solid var(--ink); background:#fff; color:var(--ink);
                 cursor:pointer; box-shadow:4px 4px 0 var(--ink); transition:transform .08s, box-shadow .08s; text-decoration:none; display:inline-block; }
  .btn:hover, button:hover { transform:translate(-1px,-1px); box-shadow:5px 5px 0 var(--ink); }
  .btn:active, button:active { transform:translate(3px,3px); box-shadow:1px 1px 0 var(--ink); }
  button:disabled { opacity:.45; cursor:not-allowed; transform:none; box-shadow:4px 4px 0 var(--ink); }
  button:focus-visible, .btn:focus-visible, a:focus-visible { outline:3px solid var(--grape); outline-offset:3px; }
  .grape { background:var(--grape); color:#fff; } .mint { background:var(--mint); } .pink { background:var(--pink); } .lemon { background:var(--lemon); }
  .big { font-size:1.15rem; padding:14px 26px; }
  .linkbtn { background:none; border:none; box-shadow:none; color:var(--muted); text-decoration:underline; font-weight:600; padding:6px; font-size:.9rem; }
  .linkbtn:hover, .linkbtn:active { box-shadow:none; transform:none; }
  .row { display:flex; gap:12px; flex-wrap:wrap; align-items:center; }
  .chip { background:var(--bg); border:2px solid var(--ink); border-radius:999px; padding:2px 12px; font-weight:700; font-size:.9rem; }
  .thumbs { display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }
  .thumbs img { width:70px; height:70px; object-fit:cover; border:3px solid var(--ink); border-radius:6px; box-shadow:3px 3px 0 var(--ink); }
  .thumbs img:nth-child(odd) { rotate:-3deg; } .thumbs img:nth-child(even) { rotate:3deg; }
  .error { color:#B3123F; font-weight:600; margin:8px 0 0; }
  #bar { display:none; height:22px; border:3px solid var(--ink); border-radius:999px; background:#fff; overflow:hidden; margin:16px 0 8px; }
  #fill { height:100%; width:0; transition:width .25s; border-right:3px solid var(--ink);
          background:repeating-linear-gradient(45deg, var(--mint) 0 12px, #7BF0CD 12px 24px); background-size:34px 34px; animation:slide 1s linear infinite; }
  @keyframes slide { to { background-position:34px 0; } }
  .count { color:var(--muted); font-size:.95rem; margin:0; }
  #fun { font-weight:700; margin:4px 0 0; min-height:1.4em; }
  #deckwrap { display:none; }
  .deckhead { display:flex; align-items:center; gap:12px; margin:6px 0 10px; }
  #deckTrack { flex:1; height:14px; border:3px solid var(--ink); border-radius:999px; overflow:hidden; background:#fff; }
  #deckBar { height:100%; width:0; background:var(--grape); transition:width .3s; }
  #stage { position:relative; height:min(62vh,540px); display:flex; align-items:center; justify-content:center; touch-action:pan-y; user-select:none; }
  .ghost { position:absolute; width:min(78%,420px); height:84%; background:#fff; border:3px solid var(--ink); border-radius:6px; display:none; }
  #stage.stack .ghost { display:block; }
  .g1 { rotate:4deg; } .g2 { rotate:-5deg; background:var(--lemon); }
  #card { position:relative; z-index:2; max-width:100%; background:#fff; border:3px solid var(--ink); border-radius:6px; padding:10px 10px 0;
          box-shadow:6px 6px 0 var(--ink); rotate:-1.2deg; cursor:grab; touch-action:pan-y; }
  #card:active { cursor:grabbing; }
  #card::before { content:""; position:absolute; top:-14px; left:50%; width:84px; height:26px; margin-left:-42px; rotate:-3deg;
                  background:rgba(92,200,255,.75); border:2px solid var(--ink); }
  #photo { display:block; max-width:100%; max-height:calc(min(62vh,540px) - 96px); object-fit:contain; pointer-events:none; -webkit-user-drag:none; }
  #cap { height:46px; display:flex; align-items:center; justify-content:center; font-family:"Segoe Print","Bradley Hand","Comic Sans MS",cursive; color:var(--muted); font-size:.95rem; }
  .tag { position:absolute; top:26px; padding:4px 14px; border:4px solid var(--ink); border-radius:10px; font-family:"Segoe UI Black","Arial Black",sans-serif;
         font-size:1.7rem; opacity:0; z-index:3; pointer-events:none; }
  #tagYes { left:14px; background:var(--mint); rotate:-12deg; } #tagNo { right:14px; background:var(--pink); rotate:12deg; }
  .deckbtns { display:flex; gap:12px; justify-content:center; margin-top:18px; flex-wrap:wrap; }
  .keys { text-align:center; margin:14px 0 0; }
  #results { display:none; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(120px,1fr)); gap:16px; margin-top:18px; }
  .pol { display:block; background:#fff; border:3px solid var(--ink); padding:5px 5px 16px; box-shadow:3px 3px 0 var(--ink); transition:transform .15s; }
  .pol img { width:100%; aspect-ratio:1; object-fit:cover; display:block; }
  .pol:nth-child(3n+1) { rotate:-2deg; } .pol:nth-child(3n+2) { rotate:1.5deg; } .pol:nth-child(3n) { rotate:-.5deg; }
  .pol:hover { rotate:0deg; transform:scale(1.05); z-index:2; }
  @media (prefers-reduced-motion: reduce) {
    .wiggle, #fill { animation:none; } * { transition:none !important; }
  }
</style></head>
<body>
<canvas id="fx"></canvas>
<div class="wrap">
  <header>
    <div>
      <h1><span class="wiggle"><img class="logo" src="/static/logo.svg" alt=""></span>FaceGotcha</h1>
      <p class="sub">Dump the group-chat photo pile and we'll hunt down the ones you're in. Nothing leaves this computer.</p>
    </div>
    <div class="hud" title="Photos of you found so far"><span class="hudnum" id="hudNum">0</span><span class="hudlbl">found</span></div>
  </header>

  <section>
    <h2><span class="badge b1">1</span>Show us your face</h2>
    <p class="hint">Pick up to 5 clear photos of you. Solo shots work best.</p>
    <input type="file" id="refInput" accept="image/*" multiple hidden>
    <button class="pink" id="refBtn">Pick your photos</button>
    <div class="thumbs" id="refThumbs"></div>
    <p class="error" id="refErr"></p>
  </section>

  <section>
    <h2><span class="badge b2">2</span>Throw in the photo pile</h2>
    <p class="hint">Add photos, or a whole folder, like your unzipped WhatsApp export.</p>
    <input type="file" id="photoInput" accept="image/*" multiple hidden>
    <input type="file" id="folderInput" webkitdirectory multiple hidden>
    <div class="row">
      <button id="photoBtn">Add photos</button>
      <button id="folderBtn">Add a folder</button>
      <span class="chip" id="photoCount">0 photos added</span>
    </div>
    <p class="error" id="photoErr"></p>
  </section>

  <section>
    <h2><span class="badge b3">3</span>Start the hunt</h2>
    <div class="row">
      <button class="grape big" id="runBtn" disabled>Find my photos</button>
      <button class="linkbtn" id="resetBtn">Start over</button>
    </div>
    <div id="bar"><div id="fill"></div></div>
    <p class="count" id="runText" aria-live="polite"></p>
    <p id="fun"></p>
    <p class="error" id="runErr"></p>
  </section>

  <section id="deckwrap">
    <h2>Is this you?</h2>
    <p class="hint">Swipe right if it's you, left if not. Check every face in the photo.</p>
    <div class="deckhead"><span class="chip" id="deckLeft"></span><div id="deckTrack"><div id="deckBar"></div></div></div>
    <div id="stage">
      <div class="ghost g2"></div><div class="ghost g1"></div>
      <div id="card">
        <img id="photo" alt="Photo to review">
        <div id="cap"></div>
        <span class="tag" id="tagYes">GOTCHA!</span><span class="tag" id="tagNo">NOPE</span>
      </div>
    </div>
    <div class="deckbtns">
      <button class="pink" id="noBtn">Nope</button>
      <button id="undoBtn">Undo</button>
      <button class="mint" id="yesBtn">That's me!</button>
    </div>
    <p class="count keys">Keys: left arrow = nope, right arrow = that's me, Z = undo</p>
  </section>

  <section id="results">
    <h2 id="resultsTitle">Hunt complete</h2>
    <p class="hint" id="resultsHint"></p>
    <a class="btn lemon big" href="/api/download" id="dl">Download as zip</a>
    <div class="grid" id="grid"></div>
  </section>
</div>

<script>
const $ = id => document.getElementById(id);
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
let st = null, current = null, busy = false, poller = null, gridKey = '';
let deckTotal = 0, celebrated = true, shown = 0, funTimer = null;
const FUN = ['Squinting at group shots...', 'Counting faces...', 'Comparing fingerprints...',
             'Ignoring photos of food...', 'Looking for that one smile...', 'Checking every corner of the frame...'];

async function api(path, opts) { return (await fetch(path, opts)).json(); }

/* ---------- confetti ---------- */
const fx = $('fx'), ctx = fx.getContext('2d'); let parts = [], raf = null;
function sizeFx() { fx.width = innerWidth; fx.height = innerHeight; }
addEventListener('resize', sizeFx); sizeFx();
function burst(x, y, n) {
  if (reduceMotion) return;
  const cols = ['#6D4AFF', '#20D6A0', '#FFD93D', '#FF5C9D', '#5CC8FF'];
  for (let i = 0; i < n; i++) {
    const a = Math.random() * Math.PI * 2, s = 4 + Math.random() * 7;
    parts.push({x, y, vx: Math.cos(a) * s, vy: Math.sin(a) * s - 5, r: 3 + Math.random() * 4,
                c: cols[i % 5], life: 70 + Math.random() * 30, rot: Math.random() * 6});
  }
  if (!raf) raf = requestAnimationFrame(tick);
}
function tick() {
  ctx.clearRect(0, 0, fx.width, fx.height);
  parts = parts.filter(p => p.life > 0);
  for (const p of parts) {
    p.x += p.vx; p.y += p.vy; p.vy += 0.35; p.vx *= 0.99; p.life--; p.rot += 0.2;
    ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.rot); ctx.fillStyle = p.c;
    ctx.fillRect(-p.r, -p.r / 2, p.r * 2, p.r); ctx.restore();
  }
  raf = parts.length ? requestAnimationFrame(tick) : null;
}

/* ---------- counter in the corner ---------- */
function countTo(to) {
  const el = $('hudNum'), from = shown; shown = to;
  if (reduceMotion || from === to) { el.textContent = to; return; }
  const t0 = performance.now();
  (function step(t) {
    const k = Math.min(1, (t - t0) / 500);
    el.textContent = Math.round(from + (to - from) * k);
    if (k < 1) requestAnimationFrame(step);
  })(t0);
}

/* ---------- uploads ---------- */
async function upload(kind, files, errEl) {
  errEl.textContent = '';
  const BATCH = 20;
  for (let i = 0; i < files.length; i += BATCH) {
    const fd = new FormData();
    files.slice(i, i + BATCH).forEach(f => fd.append('files', f, f.name));
    const r = await api('/api/upload/' + kind, {method: 'POST', body: fd});
    if (!r.ok) { errEl.textContent = r.error || 'Upload failed.'; return; }
    if (kind === 'photos') $('photoCount').textContent = 'Adding... ' + Math.min(i + BATCH, files.length) + ' of ' + files.length;
  }
  refresh();
}
$('refBtn').onclick = () => $('refInput').click();
$('photoBtn').onclick = () => $('photoInput').click();
$('folderBtn').onclick = () => $('folderInput').click();
$('refInput').onchange = e => { upload('refs', [...e.target.files], $('refErr')); e.target.value = ''; };
$('photoInput').onchange = e => { upload('photos', [...e.target.files], $('photoErr')); e.target.value = ''; };
$('folderInput').onchange = e => { upload('photos', [...e.target.files], $('photoErr')); e.target.value = ''; };

$('runBtn').onclick = async () => {
  $('runErr').textContent = '';
  const r = await api('/api/run', {method: 'POST'});
  if (!r.ok) { $('runErr').textContent = r.error; return; }
  celebrated = false; deckTotal = 0; gridKey = '';
  startPolling();
};
$('resetBtn').onclick = async () => {
  if (!confirm('Remove the uploaded copies and start over? Your original photos are not affected.')) return;
  const r = await api('/api/reset', {method: 'POST'});
  if (!r.ok) alert(r.error);
  celebrated = true; deckTotal = 0; gridKey = ''; countTo(0);
  refresh();
};

function startPolling() { if (!poller) poller = setInterval(refresh, 600); refresh(); }

async function refresh() {
  st = await api('/api/state');
  const running = st.job.state === 'running';

  $('refThumbs').innerHTML = st.refs.map(n => '<img src="/img/refs/' + encodeURIComponent(n) + '" alt="Reference photo">').join('');
  if (!(running && $('photoCount').textContent.startsWith('Adding')))
    $('photoCount').textContent = st.photos + (st.photos === 1 ? ' photo added' : ' photos added');
  $('runBtn').disabled = running || !st.refs.length || !st.photos;

  $('bar').style.display = running ? 'block' : 'none';
  if (running) {
    $('fill').style.width = (st.job.total ? 100 * st.job.done / st.job.total : 0) + '%';
    $('runText').textContent = 'Searching: ' + st.job.done + ' of ' + st.job.total + ' photos';
    if (!funTimer) {
      let i = 0; $('fun').textContent = FUN[0];
      funTimer = setInterval(() => { i = (i + 1) % FUN.length; $('fun').textContent = FUN[i]; }, 2200);
    }
  } else {
    if (funTimer) { clearInterval(funTimer); funTimer = null; }
    $('fun').textContent = '';
    if (st.job.state === 'done') {
      const c = st.job.counts;
      $('runText').textContent = 'Searched ' + c.total + ' photos. ' + c.matched + ' matched, ' + c.maybe + ' to review.';
    } else if (st.job.state === 'error') {
      $('runErr').textContent = st.job.error; $('runText').textContent = '';
    } else { $('runText').textContent = ''; }
  }
  if (!running && poller) { clearInterval(poller); poller = null; }

  countTo(st.job.state === 'done' ? st.matched : 0);

  // swipe deck
  const showDeck = st.job.state === 'done' && st.maybe > 0;
  $('deckwrap').style.display = showDeck ? 'block' : 'none';
  if (showDeck) {
    deckTotal = Math.max(deckTotal, st.maybe);
    $('deckLeft').textContent = st.maybe + ' left';
    $('deckBar').style.width = (100 * (deckTotal - st.maybe) / deckTotal) + '%';
    $('stage').classList.toggle('stack', st.maybe > 1);
    if (st.next !== current) { current = st.next; showCard(); }
  } else { current = null; }

  // results
  const showResults = st.job.state === 'done' && !st.maybe;
  $('results').style.display = showResults ? 'block' : 'none';
  if (showResults) {
    $('resultsTitle').textContent = st.matched === 0 ? 'No photos of you this time'
      : 'Hunt complete: you are in ' + st.matched + (st.matched === 1 ? ' photo' : ' photos');
    $('resultsHint').textContent = 'Copies are also saved in workspace/results/matched.';
    $('dl').style.display = st.matched ? 'inline-block' : 'none';
    const key = String(st.matched);
    if (key !== gridKey) { gridKey = key; loadGrid(); }
    if (!celebrated) {
      celebrated = true;
      if (st.matched) { burst(innerWidth * 0.3, innerHeight * 0.35, 90); burst(innerWidth * 0.7, innerHeight * 0.35, 90); }
    }
  }
}

async function loadGrid() {
  const r = await api('/api/matched');
  $('grid').innerHTML = r.names.map(n => {
    const u = '/img/matched/' + encodeURIComponent(n);
    return '<a class="pol" href="' + u + '" target="_blank" rel="noopener"><img loading="lazy" src="' + u + '" alt=""></a>';
  }).join('');
}

/* ---------- swipe deck ---------- */
function showCard() {
  const card = $('card');
  card.style.transition = 'none'; card.style.transform = ''; card.style.opacity = 1;
  $('tagYes').style.opacity = 0; $('tagNo').style.opacity = 0;
  if (current) {
    $('photo').src = '/img/maybe/' + encodeURIComponent(current);
    const sc = parseFloat(current.split('_')[0]);
    $('cap').textContent = isNaN(sc) ? '' : 'match score ' + sc.toFixed(2);
  }
}

async function decide(decision) {
  if (!current || busy) return;
  busy = true;
  const card = $('card'), dir = decision === 'me' ? 1 : -1;
  $('tagYes').style.opacity = decision === 'me' ? 1 : 0;
  $('tagNo').style.opacity = decision === 'me' ? 0 : 1;
  if (decision === 'me') { const b = card.getBoundingClientRect(); burst(b.left + b.width / 2, b.top + b.height / 2, 36); }
  if (!reduceMotion) {
    await new Promise(r => setTimeout(r, 120));
    card.style.transition = 'transform .28s ease-out, opacity .28s';
    card.style.transform = 'translateX(' + dir * innerWidth * 0.8 + 'px) rotate(' + dir * 22 + 'deg)';
    card.style.opacity = 0;
    await new Promise(r => setTimeout(r, 280));
  }
  await api('/api/decide', {method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({name: current, decision})});
  busy = false; current = null; await refresh();
}
async function undo() {
  if (busy) return;
  await api('/api/undo', {method: 'POST'}); current = null; refresh();
}
$('yesBtn').onclick = () => decide('me');
$('noBtn').onclick = () => decide('no');
$('undoBtn').onclick = undo;
document.addEventListener('keydown', e => {
  if ($('deckwrap').style.display !== 'block') return;
  if (e.key === 'ArrowRight') decide('me');
  else if (e.key === 'ArrowLeft') decide('no');
  else if (e.key.toLowerCase() === 'z') undo();
});

(() => {
  const card = $('card'); let startX = 0, dx = 0, dragging = false;
  card.addEventListener('pointerdown', e => {
    if (busy) return;
    dragging = true; startX = e.clientX; dx = 0;
    card.setPointerCapture(e.pointerId); card.style.transition = 'none';
  });
  card.addEventListener('pointermove', e => {
    if (!dragging) return;
    dx = e.clientX - startX;
    card.style.transform = 'translateX(' + dx + 'px) rotate(' + dx / 18 + 'deg)';
    $('tagYes').style.opacity = Math.max(0, Math.min(1, dx / 90));
    $('tagNo').style.opacity = Math.max(0, Math.min(1, -dx / 90));
  });
  const end = () => {
    if (!dragging) return; dragging = false;
    if (Math.abs(dx) > 110) { decide(dx > 0 ? 'me' : 'no'); }
    else {
      card.style.transition = reduceMotion ? 'none' : 'transform .2s ease-out';
      card.style.transform = ''; $('tagYes').style.opacity = 0; $('tagNo').style.opacity = 0;
    }
  };
  card.addEventListener('pointerup', end);
  card.addEventListener('pointercancel', end);
})();

refresh();
</script></body></html>
"""


@app.get("/api/matched")
def matched_names():
    return jsonify(names=[p.name for p in images_in(RESULTS / "matched")])


if __name__ == "__main__":
    WORK.mkdir(exist_ok=True)
    # 127.0.0.1 = this laptop only. Nobody else on your wifi can open the page.
    app.run(host="127.0.0.1", port=5000, debug=False)
