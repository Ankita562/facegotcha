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
@app.post("/api/move")

def move():
    """Move a finished photo between the matched and rejected albums."""
    data = request.get_json(force=True)
    name = Path(data.get("name", "")).name
    to = data.get("to")
    if to not in ("matched", "rejected"):
        return jsonify(ok=False, error="Bad destination"), 400
    src = RESULTS / ("rejected" if to == "matched" else "matched") / name
    if not src.is_file():
        return jsonify(ok=False, error="File not found"), 404
    dest_dir = RESULTS / to
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest_dir / name))
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
<link rel="icon" type="image/svg+xml" href="/static/img/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/static/img/favicon-32.png">
<link rel="apple-touch-icon" href="/static/img/apple-touch-icon.png">
   <link rel="stylesheet" href="/static/style.css">
  </head>
<body>
<canvas id="fx"></canvas>
<div id="purgeDock">
  <div class="cloud" id="purgeNote" role="note">Deletes every saved session in the workspace folder. Your original photos are not touched.</div>
  <button class="danger" id="purgeBtn" aria-describedby="purgeNote">Delete all copies</button>
</div>
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
      <h1><span class="wiggle"><img class="logo" src="/static/img/logo.svg" alt=""></span><span class="wordmark">FaceGotcha</span></h1>
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
    <p class="hint note">You can also drag a folder or photos straight onto this card. Choosing a folder with the button makes your browser ask "Upload N files to this site?". That's normal: the files only go to this app, on your own computer.</p>
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
    <p class="hint">Borderline photos you swiped "Nope" on. Photos that scored too low were never saved anywhere. Rejected one by mistake? Press "That's me" to move it back.</p>
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
  <footer id="foot">
  <p>&copy; 2026 Ankita. FaceGotcha is open source under the MIT License.</p>
  <p>Feedback or questions? <a href="mailto:skibidz0805@gmail.com?subject=FaceGotcha%20feedback">skibidz0805@gmail.com</a></p>
  <p class="tiny">Face detection and recognition use the OpenCV Zoo YuNet and SFace models. Nothing you add leaves this computer.</p>
  </footer>

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

   <script src="/static/app.js"></script>
</body></html>
"""


@app.get("/api/matched")
def matched_names():
    return jsonify(names=[p.name for p in images_in(RESULTS / "matched")])


@app.get("/api/rejected")
def rejected_names():
    return jsonify(names=[p.name for p in images_in(RESULTS / "rejected")])


if __name__ == "__main__":
    WORK.mkdir(exist_ok=True)
    import threading, webbrowser
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    # 127.0.0.1 = this laptop only. Nobody else on your wifi can open the page.
    app.run(host="127.0.0.1", port=5000, debug=False)
