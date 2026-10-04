"""
app.py - FaceGotcha: find the photos you're in. Runs ONLY on this laptop.

    python app.py        then open  http://127.0.0.1:5000

Uploads are COPIED into a new timestamped folder for every session:
    workspace/2026-10-04_14-32-10/refs      your reference photos (max 5)
    workspace/2026-10-04_14-32-10/photos    the photos to search
    workspace/2026-10-04_14-32-10/results   matched / maybe / rejected + results.csv
Your original photos are never touched. Old sessions stay on disk until you
delete them ("Delete all saved copies" at the bottom of the page).
"""

import os
import shutil
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

import find_me

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)  # find_me.py looks for models/ relative to the current folder

WORK = ROOT / "workspace"
SESSION = REFS = PHOTOS = RESULTS = None   # set by new_session() below

# Settings found by testing on a small set of photos (see the write-up).
THRESHOLD = 0.53      # at or above: auto-matched
REVIEW_FLOOR = 0.38   # between this and THRESHOLD: you decide by swiping
MAX_REFS = 5

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 ** 3  # 2 GB per request

job = {"state": "idle", "done": 0, "total": 0, "error": None, "counts": None}
undo_stack = []
_tools = None


def new_session():
    """Start a fresh session in its own timestamped folder. Folders are created on first upload."""
    global SESSION, REFS, PHOTOS, RESULTS
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder, n = WORK / stamp, 1
    while folder.exists():
        folder = WORK / f"{stamp}_{n}"
        n += 1
    SESSION = folder
    REFS, PHOTOS, RESULTS = folder / "refs", folder / "photos", folder / "results"
    undo_stack.clear()
    job.update(state="idle", done=0, total=0, error=None, counts=None)


new_session()


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
    # Every visit starts an empty session in a new timestamped folder (old ones are kept).
    if job["state"] != "running":
        new_session()
    return PAGE


@app.get("/api/state")
def state():
    pending = images_in(RESULTS / "maybe")
    return jsonify(
        refs=[p.name for p in images_in(REFS)],
        photos=len(images_in(PHOTOS)),
        job=job,
        session=SESSION.name,
        maybe=len(pending),
        matched=len(images_in(RESULTS / "matched")),
        rejected=len(images_in(RESULTS / "rejected")),
        next=(pending[-1].name if pending else None),
    )


@app.get("/img/<folder>/<path:name>")
def img(folder, name):
    if folder not in ("refs", "maybe", "matched", "rejected"):
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
@app.get("/api/download/<which>")
def download(which="matched"):
    if which not in ("matched", "rejected"):
        return "Not found", 404
    folder = RESULTS / which
    if not images_in(folder):
        return "Nothing to download yet.", 404
    name = "my_photos" if which == "matched" else "rejected_photos"
    base = Path(tempfile.mkdtemp()) / name
    zip_path = shutil.make_archive(str(base), "zip", folder)
    return send_file(zip_path, as_attachment=True, download_name=f"{name}.zip")


def workspace_stats():
    """How many saved sessions, files and bytes are in the workspace folder."""
    sessions = [p for p in WORK.iterdir() if p.is_dir()] if WORK.is_dir() else []
    files = [f for p in sessions for f in p.rglob("*") if f.is_file()]
    return len(sessions), len(files), sum(f.stat().st_size for f in files)


@app.get("/api/workspace")
def workspace():
    n, files, size = workspace_stats()
    return jsonify(sessions=n, files=files, mb=round(size / 1048576, 1))


@app.post("/api/reset")
def reset():
    if job["state"] == "running":
        return jsonify(ok=False, error="Wait for the search to finish."), 409
    old = SESSION
    new_session()                                   # the old session stays saved on disk
    return jsonify(ok=True, previous=old.name if old.exists() else None)


@app.post("/api/purge")
def purge():
    if job["state"] == "running":
        return jsonify(ok=False, error="Wait for the search to finish."), 409
    n, files, size = workspace_stats()              # count first, so the page can say what was removed
    shutil.rmtree(WORK, ignore_errors=True)         # only our copies; originals are elsewhere
    WORK.mkdir(exist_ok=True)
    new_session()
    return jsonify(ok=True, sessions=n, files=files, mb=round(size / 1048576, 1))


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
  .wrap { max-width:1120px; margin:0 auto; padding:28px 16px 72px; }
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
  .reelwrap { display:flex; align-items:center; gap:8px; margin-top:16px; }
  .reel { flex:1; min-width:0; display:flex; gap:16px; overflow-x:auto; scroll-snap-type:x proximity; padding:12px 8px 20px; scrollbar-width:thin; }
  .reel .pol { flex:none; width:150px; scroll-snap-align:start; }
  .reel .pol:first-child { margin-left:auto; }   /* auto margins center a short reel but still let a long one scroll */
  .reel .pol:last-child { margin-right:auto; }
  .arrow { padding:4px 14px 8px; font-size:1.6rem; line-height:1; flex:none; }
  #stats, #rejected { display:none; }
  #statbar { display:flex; height:26px; border:3px solid var(--ink); border-radius:999px; overflow:hidden; background:#fff; margin-top:6px; }
  .seg { height:100%; border-right:2px solid var(--ink); } .seg:last-child { border-right:none; }
  #legend { display:flex; flex-wrap:wrap; gap:6px 18px; margin:12px 0 4px; font-size:.95rem; }
  .dot { display:inline-block; width:13px; height:13px; border-radius:50%; border:2px solid var(--ink); margin-right:6px; vertical-align:-1px; }
  #rate { font-weight:800; font-size:1.1rem; margin:10px 0 6px; }
  #overlay { display:none; position:fixed; inset:0; background:rgba(26,21,48,.62); z-index:60; align-items:center; justify-content:center; padding:16px; }
  #ovPanel { background:#fff; border:3px solid var(--ink); border-radius:16px; box-shadow:6px 6px 0 var(--ink); width:min(980px,100%); max-height:90vh; overflow:auto; padding:0 18px 18px; }
  .ovhead { position:sticky; top:0; background:#fff; display:flex; justify-content:space-between; align-items:center; padding:16px 0 10px; z-index:1; }
  .ovhead h2 { margin:0; }
  .purge { display:flex; flex-direction:column; align-items:center; margin-top:18px; }
  .cloud { position:relative; max-width:340px; text-align:center; background:#fff; border:3px solid var(--ink); border-radius:26px;
           padding:10px 18px; font-size:.9rem; font-weight:600; box-shadow:4px 4px 0 var(--ink); margin-bottom:36px; }
  .cloud::before, .cloud::after { content:""; position:absolute; background:#fff; border:3px solid var(--ink); border-radius:50%; left:50%; }
  .cloud::before { width:16px; height:16px; bottom:-22px; margin-left:-8px; }
  .cloud::after { width:9px; height:9px; bottom:-34px; margin-left:-4px; }
  button.danger { background:var(--pink); }
  html { scroll-behavior:smooth; }
  section { scroll-margin-top:100px; }
  #nav { position:sticky; top:0; z-index:40; background:var(--bg); border-bottom:3px solid var(--ink); }
  .navin { max-width:1120px; margin:0 auto; padding:10px 16px; display:flex; gap:8px; align-items:center; overflow-x:auto; white-space:nowrap; }
  .navlink { color:var(--ink); text-decoration:none; font-weight:700; font-size:.95rem; border:2px solid var(--ink); border-radius:999px;
             padding:4px 14px; background:#fff; flex:none; }
  .navlink:hover { background:var(--lemon); }
  .navlink.active { background:var(--grape); color:#fff; }
  .navlink.off { opacity:.4; pointer-events:none; }
  .navlink:focus-visible { outline:3px solid var(--grape); outline-offset:2px; }
  .navfound { margin-left:auto; flex:none; font-weight:700; font-size:.95rem; padding-left:12px; }
  .navfound b { font-family:"Segoe UI Black","Arial Black",sans-serif; }
  .twocol { display:flex; flex-wrap:wrap; gap:20px; margin-bottom:20px; }
  .twocol > section { flex:1 1 380px; min-width:0; margin-bottom:0; }
  #deckwrap { max-width:820px; margin:0 auto 20px; }
  #dlg { display:none; position:fixed; inset:0; background:rgba(26,21,48,.62); z-index:70; align-items:center; justify-content:center; padding:16px; }
  .dlgbox { background:#fff; border:3px solid var(--ink); border-radius:16px; box-shadow:6px 6px 0 var(--ink); max-width:460px; width:100%; padding:22px; }
  .dlgbox h2 { margin:0 0 8px; font-size:1.3rem; }
  .dlgbox p { margin:0 0 20px; color:var(--muted); }
  .dlgbox .row { justify-content:flex-end; }
  #toast { position:fixed; left:50%; bottom:24px; transform:translate(-50%,30px); opacity:0; pointer-events:none; z-index:80; max-width:min(580px,92vw);
           background:var(--mint); border:3px solid var(--ink); border-radius:14px; box-shadow:4px 4px 0 var(--ink); padding:12px 18px; font-weight:700;
           cursor:pointer; transition:opacity .2s, transform .2s; }
  #toast.show { opacity:1; transform:translate(-50%,0); pointer-events:auto; }
  #steps { list-style:none; margin:0; padding:0; display:flex; align-items:center; flex:none; }
  #steps li { display:flex; align-items:center; }
  #steps li + li::before { content:""; width:30px; height:5px; border-radius:3px; background:#D9D1F5; margin:0 6px; flex:none; }
  #steps li.filled::before { background:var(--ink); }
  .step { display:flex; align-items:center; gap:8px; text-decoration:none; color:var(--ink); font-weight:700; font-size:.95rem; padding:2px 4px; border-radius:999px; }
  .dotc { width:30px; height:30px; border-radius:50%; border:3px solid var(--ink); display:grid; place-items:center; font-weight:900;
          font-size:.9rem; background:#fff; flex:none; }
  .step.done .dotc { background:var(--mint); }
  .step.current .dotc { background:var(--lemon); box-shadow:0 0 0 4px rgba(109,74,255,.28); }
  .step.todo { color:var(--muted); } .step.todo .dotc { border-color:#9F98B8; color:var(--muted); }
  .step.active .lab { text-decoration:underline; text-decoration-thickness:3px; text-underline-offset:5px; text-decoration-color:var(--grape); }
  .step.off { pointer-events:none; }
  .step.running .dotc { animation:pulse 1s ease-in-out infinite; }
  @keyframes pulse { 50% { transform:scale(1.14); } }
  #meter { height:12px; background:#fff; border-top:3px solid var(--ink); }
  #meterFill { height:100%; width:0; border-right:0 solid var(--ink); transition:width .35s;
               background:repeating-linear-gradient(45deg, var(--mint) 0 10px, #7BF0CD 10px 20px); }
  #meterFill.complete { background:var(--lemon); }
  @media (max-width:820px) { .step:not(.current) .lab { display:none; } #steps li + li::before { width:14px; } }
  @media (prefers-reduced-motion: reduce) {
    html { scroll-behavior:auto; } .step.running .dotc { animation:none; }
    .wiggle, #fill { animation:none; } * { transition:none !important; }
  }
</style></head>
<body>
<canvas id="fx"></canvas>
<nav id="nav" aria-label="Progress and sections">
  <div class="navin">
    <ol id="steps">
      <li><a class="step todo" href="#sec-face" data-sec="sec-face" data-step="face"><span class="dotc">1</span><span class="lab">Your face</span></a></li>
      <li><a class="step todo" href="#sec-photos" data-sec="sec-photos" data-step="photos"><span class="dotc">2</span><span class="lab">Photo pile</span></a></li>
      <li><a class="step todo" href="#sec-hunt" data-sec="sec-hunt" data-step="hunt"><span class="dotc">3</span><span class="lab">The hunt</span></a></li>
      <li><a class="step todo off" href="#deckwrap" data-sec="deckwrap" data-step="review" aria-disabled="true" tabindex="-1"><span class="dotc">4</span><span class="lab">Review</span></a></li>
      <li><a class="step todo off" href="#results" data-sec="results" data-step="done" aria-disabled="true" tabindex="-1"><span class="dotc">5</span><span class="lab">Done</span></a></li>
    </ol>
    <a class="navlink off" href="#stats" data-sec="stats" aria-disabled="true" tabindex="-1">Numbers</a>
    <a class="navlink off" href="#rejected" data-sec="rejected" aria-disabled="true" tabindex="-1">Rejected</a>
    <span class="navfound"><b id="navNum">0</b> found</span>
  </div>
  <div id="meter" role="progressbar" aria-label="Progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="meterFill"></div></div>
</nav>
<div class="wrap">
  <header>
    <div>
      <h1><span class="wiggle"><img class="logo" src="/static/logo.svg" alt=""></span>FaceGotcha</h1>
      <p class="sub">Dump the group-chat photo pile and we'll hunt down the ones you're in. Nothing leaves this computer.</p>
    </div>
    <div class="hud" title="Photos of you found so far"><span class="hudnum" id="hudNum">0</span><span class="hudlbl">found</span></div>
  </header>

  <div class="twocol">
  <section id="sec-face">
    <h2><span class="badge b1">1</span>Show us your face</h2>
    <p class="hint">Pick up to 5 clear photos of you. Solo shots work best.</p>
    <input type="file" id="refInput" accept="image/*" multiple hidden>
    <button class="pink" id="refBtn">Pick your photos</button>
    <div class="thumbs" id="refThumbs"></div>
    <p class="error" id="refErr"></p>
  </section>

  <section id="sec-photos">
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
    <p class="hint" style="margin:8px 0 0">Choosing a folder makes your browser ask "Upload N files to this site?". That's normal: the files only go to this app, on your own computer.</p>
  </section>

  </div>

  <section id="sec-hunt">
    <h2><span class="badge b3">3</span>Start the hunt</h2>
    <div class="row">
      <button class="grape big" id="runBtn" disabled>Find my photos</button>
      <button id="resetBtn" title="Begin a new empty session. The current one stays saved.">Start over</button>
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
    <div class="row">
      <a class="btn lemon big" href="/api/download" id="dl">Download as zip</a>
      <button id="viewMatched">View all</button>
    </div>
    <div class="reelwrap" id="matchedWrap">
      <button class="arrow" data-target="reel" data-dir="-1" aria-label="Scroll left">&#8249;</button>
      <div class="reel" id="reel"></div>
      <button class="arrow" data-target="reel" data-dir="1" aria-label="Scroll right">&#8250;</button>
    </div>
  </section>

  <div class="twocol">
  <section id="stats">
    <h2>The numbers</h2>
    <p class="hint" id="statsHint"></p>
    <div id="statbar"></div>
    <div id="legend"></div>
    <p id="rate"></p>
    <p class="count">These show how your photos were sorted and what you confirmed in review. They are not an accuracy score, because FaceGotcha has no answer key to check against.</p>
  </section>

  <section id="rejected">
    <h2>Rejected album</h2>
    <p class="hint">Borderline photos you swiped "Nope" on. Photos that scored too low were never saved anywhere.</p>
    <div class="row">
      <a class="btn pink" href="/api/download/rejected">Download as zip</a>
      <button id="viewRejected">View all</button>
    </div>
    <div class="reelwrap">
      <button class="arrow" data-target="reelR" data-dir="-1" aria-label="Scroll left">&#8249;</button>
      <div class="reel" id="reelR"></div>
      <button class="arrow" data-target="reelR" data-dir="1" aria-label="Scroll right">&#8250;</button>
    </div>
  </section>
  </div>

  <div class="purge">
    <div class="cloud" id="purgeNote" role="note">Deletes every saved session in the workspace folder. Your original photos are not touched.</div>
    <button class="danger" id="purgeBtn" aria-describedby="purgeNote">Delete all saved copies</button>
  </div>
</div>

<div id="overlay" role="dialog" aria-modal="true" aria-labelledby="ovTitle">
  <div id="ovPanel">
    <div class="ovhead"><h2 id="ovTitle"></h2><button id="ovClose">Close</button></div>
    <div class="grid" id="ovGrid"></div>
  </div>
</div>

<div id="dlg" role="alertdialog" aria-modal="true" aria-labelledby="dlgTitle" aria-describedby="dlgText">
  <div class="dlgbox">
    <h2 id="dlgTitle"></h2>
    <p id="dlgText"></p>
    <div class="row"><button id="dlgCancel">Cancel</button><button id="dlgOk">OK</button></div>
  </div>
</div>
<div id="toast" role="status" aria-live="polite"></div>

<script>
const $ = id => document.getElementById(id);
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
let st = null, current = null, busy = false, poller = null, gridKey = '';
let deckTotal = 0, celebrated = true, shown = 0, funTimer = null, deckSeen = false, resultsSeen = false;
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
  const put = v => { $('hudNum').textContent = v; $('navNum').textContent = v; };
  const from = shown; shown = to;
  if (reduceMotion || from === to) { put(to); return; }
  const t0 = performance.now();
  (function step(t) {
    const k = Math.min(1, (t - t0) / 500);
    put(Math.round(from + (to - from) * k));
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
  celebrated = false; deckTotal = 0; gridKey = ''; deckSeen = false; resultsSeen = false;
  startPolling();
};
/* ---------- friendly confirm box and message ---------- */
let dlgResolve = null, dlgReturnFocus = null, toastTimer = null;
function askConfirm(title, text, okLabel, danger) {
  return new Promise(resolve => {
    dlgResolve = resolve; dlgReturnFocus = document.activeElement;
    $('dlgTitle').textContent = title; $('dlgText').textContent = text;
    $('dlgOk').textContent = okLabel; $('dlgOk').className = danger ? 'pink' : 'mint';
    $('dlg').style.display = 'flex'; document.body.style.overflow = 'hidden';
    $('dlgCancel').focus();   // the safe choice is focused first
  });
}
function closeDlg(answer) {
  if (!dlgResolve) return;
  $('dlg').style.display = 'none'; document.body.style.overflow = '';
  const done = dlgResolve; dlgResolve = null;
  if (dlgReturnFocus) dlgReturnFocus.focus();
  done(answer);
}
$('dlgOk').onclick = () => closeDlg(true);
$('dlgCancel').onclick = () => closeDlg(false);
$('dlg').addEventListener('click', e => { if (e.target === $('dlg')) closeDlg(false); });
document.addEventListener('keydown', e => {
  if (!dlgResolve) return;
  if (e.key === 'Escape') closeDlg(false);
  else if (e.key === 'Tab') {   // keep keyboard focus inside the box
    e.preventDefault();
    ($('dlgOk') === document.activeElement ? $('dlgCancel') : $('dlgOk')).focus();
  }
});
function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 7000);
}
$('toast').onclick = () => $('toast').classList.remove('show');

$('resetBtn').onclick = async () => {
  const ok = await askConfirm('Start a new session?',
    'The current photos stay saved in the workspace folder. You will start with an empty page.', 'Start new session', false);
  if (!ok) return;
  const r = await api('/api/reset', {method: 'POST'});
  if (!r.ok) { toast(r.error || 'Could not start a new session.'); return; }
  celebrated = true; deckTotal = 0; gridKey = ''; countTo(0);
  toast(r.previous ? 'New session started. Your previous one is saved in workspace/' + r.previous + '.' : 'New session started.');
  refresh();
};
$('purgeBtn').onclick = async () => {
  const w = await api('/api/workspace');
  if (!w.sessions) { toast('Nothing to delete: there are no saved copies.'); return; }
  const ok = await askConfirm('Delete all saved copies?',
    'This permanently deletes ' + w.sessions + (w.sessions === 1 ? ' saved session' : ' saved sessions') +
    ' (' + w.files + ' files, ' + w.mb + ' MB) from the workspace folder. Your original photos are not touched.',
    'Delete everything', true);
  if (!ok) return;
  const r = await api('/api/purge', {method: 'POST'});
  if (!r.ok) { toast(r.error || 'Could not delete.'); return; }
  celebrated = true; deckTotal = 0; gridKey = ''; countTo(0);
  toast('Deleted ' + r.sessions + (r.sessions === 1 ? ' session' : ' sessions') + ' (' + r.files + ' files, ' + r.mb +
        ' MB) from the workspace folder. Your original photos are untouched.');
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
  $('stats').style.display = showResults ? 'block' : 'none';
  $('rejected').style.display = showResults && st.rejected ? 'block' : 'none';
  if (showResults) {
    $('resultsTitle').textContent = st.matched === 0 ? 'No photos of you this time'
      : 'Hunt complete: you are in ' + st.matched + (st.matched === 1 ? ' photo' : ' photos');
    $('resultsHint').textContent = 'Copies are saved in workspace/' + st.session + '/results/matched.';
    $('dl').style.display = st.matched ? 'inline-block' : 'none';
    renderStats();
    const key = st.matched + '/' + st.rejected;
    if (key !== gridKey) { gridKey = key; loadReels(); }
    if (!celebrated) {
      celebrated = true;
      if (st.matched) { burst(innerWidth * 0.3, innerHeight * 0.35, 90); burst(innerWidth * 0.7, innerHeight * 0.35, 90); }
    }
  }

  // jump to whatever just became available, so nobody has to hunt for it
  if (showDeck && !deckSeen) { deckSeen = true; goTo('deckwrap'); }
  if (showResults && !resultsSeen) { resultsSeen = true; goTo('results'); }
  updateNav();
}

function goTo(id) { $(id).scrollIntoView({behavior: reduceMotion ? 'auto' : 'smooth', block: 'start'}); }

const STEP_ORDER = ['face', 'photos', 'hunt', 'review', 'done'];

// Works out which steps are finished and how far along the whole job is.
function computeSteps(st, deckTotal) {
  const j = st.job, jobDone = j.state === 'done', running = j.state === 'running';
  const finished = jobDone && st.maybe === 0;
  const state = {face: st.refs.length > 0, photos: st.photos > 0, hunt: jobDone, review: finished, done: finished};
  const first = STEP_ORDER.find(k => !state[k]);            // the step you are on now
  let partial = 0;
  if (running && j.total) partial = j.done / j.total;
  else if (jobDone && st.maybe > 0 && deckTotal) partial = (deckTotal - st.maybe) / deckTotal;
  const doneCount = STEP_ORDER.filter(k => state[k]).length;
  const pct = Math.min(100, Math.round(100 * (doneCount + partial) / STEP_ORDER.length));
  return {state, first, pct, running, jobDone};
}

function updateSteps() {
  if (!st) return;
  const c = computeSteps(st, deckTotal);
  document.querySelectorAll('.step').forEach((a, i) => {
    const k = a.dataset.step;
    a.classList.toggle('done', c.state[k]);
    a.classList.toggle('current', k === c.first);
    a.classList.toggle('todo', !c.state[k] && k !== c.first);
    a.classList.toggle('running', c.running && k === 'hunt');
    a.querySelector('.dotc').textContent = c.state[k] ? '\u2713' : String(i + 1);
    a.parentElement.classList.toggle('filled', i > 0 && c.state[STEP_ORDER[i - 1]]);
  });
  const lab = k => document.querySelector('.step[data-step="' + k + '"] .lab');
  lab('hunt').textContent = c.running && st.job.total ? 'The hunt ' + Math.round(100 * st.job.done / st.job.total) + '%' : 'The hunt';
  lab('review').textContent = c.jobDone && st.maybe > 0 ? 'Review: ' + st.maybe + ' left' : 'Review';
  lab('done').textContent = c.pct === 100 ? 'All done!' : 'Done';
  const fill = $('meterFill');
  fill.style.width = c.pct + '%';
  fill.classList.toggle('complete', c.pct === 100);
  fill.style.borderRightWidth = c.pct > 0 && c.pct < 100 ? '3px' : '0';
  $('meter').setAttribute('aria-valuenow', String(c.pct));
}

function updateNav() {
  document.querySelectorAll('[data-sec]').forEach(a => {
    const on = getComputedStyle($(a.dataset.sec)).display !== 'none';
    a.classList.toggle('off', !on);
    a.tabIndex = on ? 0 : -1;
    a.setAttribute('aria-disabled', String(!on));
  });
  updateSteps();
}
// highlight the section you are looking at
const spy = new IntersectionObserver(entries => {
  entries.forEach(en => {
    if (en.isIntersecting)
      document.querySelectorAll('[data-sec]').forEach(a => a.classList.toggle('active', a.dataset.sec === en.target.id));
  });
}, {rootMargin: '-20% 0px -65% 0px'});
document.querySelectorAll('[data-sec]').forEach(a => spy.observe($(a.dataset.sec)));

function polaroid(which, n) {
  const u = '/img/' + which + '/' + encodeURIComponent(n);
  return '<a class="pol" href="' + u + '" target="_blank" rel="noopener"><img loading="lazy" src="' + u + '" alt=""></a>';
}

function updateArrows() {
  document.querySelectorAll('.reelwrap').forEach(w => {
    const reel = w.querySelector('.reel');
    const overflow = reel.scrollWidth > reel.clientWidth + 2;
    w.querySelectorAll('.arrow').forEach(b => b.style.visibility = overflow ? 'visible' : 'hidden');
  });
}
addEventListener('resize', updateArrows);

async function loadReels() {
  const [m, r] = await Promise.all([api('/api/matched'), api('/api/rejected')]);
  $('reel').innerHTML = m.names.map(n => polaroid('matched', n)).join('');
  $('reelR').innerHTML = r.names.map(n => polaroid('rejected', n)).join('');
  $('viewMatched').textContent = 'View all (' + m.names.length + ')';
  $('viewRejected').textContent = 'View all (' + r.names.length + ')';
  $('matchedWrap').style.display = m.names.length ? 'flex' : 'none';
  $('viewMatched').style.display = m.names.length ? 'inline-block' : 'none';
  updateArrows();
}

document.querySelectorAll('.arrow').forEach(b => b.onclick = () => {
  const el = $(b.dataset.target);
  el.scrollBy({left: Number(b.dataset.dir) * el.clientWidth * 0.8, behavior: reduceMotion ? 'auto' : 'smooth'});
});

async function openAll(which) {
  const r = await api('/api/' + which);
  $('ovTitle').textContent = (which === 'matched' ? 'All photos of you' : 'Rejected photos') + ' (' + r.names.length + ')';
  $('ovGrid').innerHTML = r.names.map(n => polaroid(which, n)).join('');
  $('overlay').style.display = 'flex'; document.body.style.overflow = 'hidden'; $('ovClose').focus();
}
function closeAll() { $('overlay').style.display = 'none'; document.body.style.overflow = ''; }
$('viewMatched').onclick = () => openAll('matched');
$('viewRejected').onclick = () => openAll('rejected');
$('ovClose').onclick = closeAll;
$('overlay').addEventListener('click', e => { if (e.target === $('overlay')) closeAll(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeAll(); });

function renderStats() {
  const c = st.job.counts; if (!c) return;
  const accepted = Math.max(0, st.matched - c.matched), rejected = st.rejected;
  const segs = [['Matched automatically', c.matched, '#20D6A0'], ['Borderline, you said yes', accepted, '#6D4AFF'],
                ['Borderline, you said no', rejected, '#FF5C9D'], ['Ignored: not you', c.no_match, '#C9C1EA'],
                ['No face found', c.no_face + c.unreadable, '#FFD93D']];
  $('statsHint').textContent = 'Searched ' + c.total + (c.total === 1 ? ' photo.' : ' photos.');
  $('statbar').innerHTML = segs.filter(s => s[1] > 0).map(s =>
    '<div class="seg" title="' + s[0] + ': ' + s[1] + '" style="width:' + (100 * s[1] / c.total) + '%;background:' + s[2] + '"></div>').join('');
  $('legend').innerHTML = segs.map(s =>
    '<span><i class="dot" style="background:' + s[2] + '"></i>' + s[0] + ': <b>' + s[1] + '</b></span>').join('');
  $('rate').textContent = c.maybe
    ? 'Of ' + c.maybe + ' borderline photos, ' + accepted + ' were really you (' + Math.round(100 * accepted / c.maybe) + '%).'
    : 'No borderline photos this time, so nothing needed review.';
}

addEventListener('beforeunload', e => {
  if (st && st.job.state === 'done' && st.maybe > 0) { e.preventDefault(); e.returnValue = ''; }
});

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
  if ($('deckwrap').style.display !== 'block' || dlgResolve) return;
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


@app.get("/api/rejected")
def rejected_names():
    return jsonify(names=[p.name for p in images_in(RESULTS / "rejected")])


if __name__ == "__main__":
    WORK.mkdir(exist_ok=True)
    # 127.0.0.1 = this laptop only. Nobody else on your wifi can open the page.
    app.run(host="127.0.0.1", port=5000, debug=False)
