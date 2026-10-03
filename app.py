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
        target = dest / name
        n = 1
        while target.exists():                     # same name twice? keep both
            target = dest / f"{Path(name).stem}_{n}{Path(name).suffix}"
            n += 1
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
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FaceGotcha</title>
<style>
  :root { --paper:#F1F3F5; --ink:#1B2430; --muted:#5B6673; --teal:#0F766E; --red:#B4412F; --line:#C9D0D8; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--paper); color:var(--ink);
         font-family:"Segoe UI", system-ui, -apple-system, sans-serif; line-height:1.45; }
  .wrap { max-width:760px; margin:0 auto; padding:24px 16px 64px; }
  h1 { font-size:1.6rem; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 24px; }
  section { background:#fff; border:1px solid var(--line); border-radius:6px; padding:16px; margin-bottom:16px; }
  h2 { font-size:1.05rem; margin:0 0 4px; }
  .hint { color:var(--muted); font-size:.92rem; margin:0 0 12px; }
  button, .btn { font:inherit; padding:11px 16px; border-radius:6px; cursor:pointer; border:2px solid var(--ink);
                 background:transparent; color:var(--ink); display:inline-block; text-decoration:none; }
  button.primary { background:var(--teal); border-color:var(--teal); color:#fff; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button:focus-visible, .btn:focus-visible { outline:3px solid #7AA7D9; outline-offset:2px; }
  .thumbs { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
  .thumbs img { width:64px; height:64px; object-fit:cover; border-radius:4px; border:1px solid var(--line); }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  .count { color:var(--muted); font-size:.95rem; }
  #bar { height:10px; background:var(--line); border-radius:5px; overflow:hidden; margin:12px 0 6px; display:none; }
  #fill { height:100%; width:0; background:var(--teal); transition:width .2s; }
  .error { color:var(--red); margin:8px 0 0; }
  #deckwrap { display:none; }
  #stage { position:relative; height:min(64vh,520px); display:flex; align-items:center; justify-content:center;
           touch-action:pan-y; user-select:none; }
  #card { position:relative; max-width:100%; max-height:100%; background:#fff; border:1px solid var(--line);
          border-radius:8px; padding:8px; box-shadow:0 8px 24px rgba(27,36,48,.14); cursor:grab; touch-action:pan-y; }
  #card:active { cursor:grabbing; }
  #photo { display:block; max-width:100%; max-height:calc(min(64vh,520px) - 20px); object-fit:contain;
           pointer-events:none; -webkit-user-drag:none; }
  .tag { position:absolute; top:18px; padding:6px 12px; border:3px solid; border-radius:6px; font-weight:700;
         font-size:1.2rem; opacity:0; background:rgba(255,255,255,.85); }
  #tagYes { left:18px; color:var(--teal); border-color:var(--teal); transform:rotate(-8deg); }
  #tagNo  { right:18px; color:var(--red); border-color:var(--red); transform:rotate(8deg); }
  .deckbtns { display:flex; gap:10px; justify-content:center; margin-top:14px; }
  .deckbtns button { min-width:120px; }
  .no { border-color:var(--red); color:var(--red); }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(110px,1fr)); gap:8px; margin-top:12px; }
  .grid img { width:100%; aspect-ratio:1; object-fit:cover; border-radius:4px; border:1px solid var(--line); }
  #results { display:none; }
  .linkbtn { background:none; border:none; color:var(--muted); text-decoration:underline; padding:0; font-size:.9rem; }
</style></head>
<body><div class="wrap">
  <h1>FaceGotcha</h1>
  <p class="sub">Find the photos you're in. Everything stays on this computer.</p>

  <section>
    <h2>1. Your face</h2>
    <p class="hint">Choose up to 5 clear photos of the same person. Solo photos work best.</p>
    <input type="file" id="refInput" accept="image/*" multiple hidden>
    <button id="refBtn">Choose reference photos</button>
    <div class="thumbs" id="refThumbs"></div>
    <p class="error" id="refErr"></p>
  </section>

  <section>
    <h2>2. Photos to search</h2>
    <p class="hint">Add photos, or a whole folder, such as your unzipped WhatsApp export.</p>
    <input type="file" id="photoInput" accept="image/*" multiple hidden>
    <input type="file" id="folderInput" webkitdirectory multiple hidden>
    <div class="row">
      <button id="photoBtn">Add photos</button>
      <button id="folderBtn">Add a folder</button>
      <span class="count" id="photoCount">0 photos added</span>
    </div>
    <p class="error" id="photoErr"></p>
  </section>

  <section>
    <div class="row">
      <button class="primary" id="runBtn" disabled>Find my photos</button>
      <button class="linkbtn" id="resetBtn">Start over</button>
    </div>
    <div id="bar"><div id="fill"></div></div>
    <p class="count" id="runText"></p>
    <p class="error" id="runErr"></p>
  </section>

  <section id="deckwrap">
    <h2>Is this you?</h2>
    <p class="hint" id="deckHint">Swipe right if it's you, left if not. Check every face in the photo.</p>
    <div id="stage">
      <div id="card">
        <img id="photo" alt="Photo to review">
        <span class="tag" id="tagYes">Me</span><span class="tag" id="tagNo">Not me</span>
      </div>
    </div>
    <div class="deckbtns">
      <button class="no" id="noBtn">Not me</button>
      <button id="undoBtn">Undo</button>
      <button class="primary" id="yesBtn">That's me</button>
    </div>
    <p class="count" style="text-align:center">Keys: left arrow = not me, right arrow = me, Z = undo</p>
  </section>

  <section id="results">
    <h2 id="resultsTitle">Your photos</h2>
    <p class="hint" id="resultsHint"></p>
    <a class="btn primary" href="/api/download" id="dl">Download as zip</a>
    <div class="grid" id="grid"></div>
  </section>
</div>

<script>
const $ = id => document.getElementById(id);
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
let st = null, current = null, busy = false, poller = null, gridKey = '';

async function api(path, opts) { return (await fetch(path, opts)).json(); }

async function upload(kind, files, errEl) {
  errEl.textContent = '';
  const BATCH = 20;
  for (let i = 0; i < files.length; i += BATCH) {
    const fd = new FormData();
    files.slice(i, i + BATCH).forEach(f => fd.append('files', f, f.name));
    const r = await api('/api/upload/' + kind, {method: 'POST', body: fd});
    if (!r.ok) { errEl.textContent = r.error || 'Upload failed.'; return; }
    if (kind === 'photos') { $('photoCount').textContent = 'Adding... ' + Math.min(i + BATCH, files.length) + ' of ' + files.length; }
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
  startPolling();
};
$('resetBtn').onclick = async () => {
  if (!confirm('Remove the uploaded copies and start over? Your original photos are not affected.')) return;
  const r = await api('/api/reset', {method: 'POST'});
  if (!r.ok) alert(r.error);
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
  } else if (st.job.state === 'done') {
    const c = st.job.counts;
    $('runText').textContent = 'Searched ' + c.total + ' photos. ' + c.matched + ' matched, ' + c.maybe + ' to review.';
  } else if (st.job.state === 'error') {
    $('runErr').textContent = st.job.error; $('runText').textContent = '';
  } else { $('runText').textContent = ''; }
  if (!running && poller) { clearInterval(poller); poller = null; }

  // review deck
  const showDeck = st.job.state === 'done' && st.maybe > 0;
  $('deckwrap').style.display = showDeck ? 'block' : 'none';
  if (showDeck && st.next !== current) { current = st.next; showCard(); }
  if (!showDeck) current = null;
  $('deckHint').textContent = 'Swipe right if it\\'s you, left if not. ' + st.maybe + ' left. Check every face in the photo.';

  // results grid
  const showResults = st.job.state === 'done' && !st.maybe;
  $('results').style.display = showResults ? 'block' : 'none';
  if (showResults) {
    $('resultsTitle').textContent = st.matched + (st.matched === 1 ? ' photo of you' : ' photos of you');
    $('resultsHint').textContent = 'Copies are also saved in workspace/results/matched.';
    $('dl').style.display = st.matched ? 'inline-block' : 'none';
    const key = String(st.matched);
    if (key !== gridKey) { gridKey = key; loadGrid(); }
  }
}

async function loadGrid() {
  // ask the server which photos are in the matched folder
  const r = await api('/api/matched');
  $('grid').innerHTML = r.names.map(n => '<img loading="lazy" src="/img/matched/' + encodeURIComponent(n) + '" alt="">').join('');
}

function showCard() {
  const card = $('card');
  card.style.transition = 'none'; card.style.transform = ''; card.style.opacity = 1;
  $('tagYes').style.opacity = 0; $('tagNo').style.opacity = 0;
  if (current) $('photo').src = '/img/maybe/' + encodeURIComponent(current);
}

async function decide(decision) {
  if (!current || busy) return;
  busy = true;
  const card = $('card'), dir = decision === 'me' ? 1 : -1;
  if (!reduceMotion) {
    card.style.transition = 'transform .25s ease-out, opacity .25s';
    card.style.transform = 'translateX(' + dir * window.innerWidth * 0.8 + 'px) rotate(' + dir * 22 + 'deg)';
    card.style.opacity = 0;
    await new Promise(r => setTimeout(r, 250));
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

// drag to swipe
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
    card.style.transform = 'translateX(' + dx + 'px) rotate(' + dx / 20 + 'deg)';
    $('tagYes').style.opacity = Math.max(0, Math.min(1, dx / 100));
    $('tagNo').style.opacity = Math.max(0, Math.min(1, -dx / 100));
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
