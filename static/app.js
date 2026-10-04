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
$('folderBtn').onclick = async () => {
  const ok = await askConfirm('Add a whole folder?',
    'Next, your browser will ask "Upload N files to this site?". Press Upload. That is normal: the files only go to FaceGotcha, on your own computer, and your originals are never changed.',
    'Choose folder', false);
  if (ok) $('folderInput').click();
};
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
/* ---------- drag a folder or photos onto the page ---------- */
async function readEntry(entry, out) {
  if (entry.isFile) {
    out.push(await new Promise((res, rej) => entry.file(res, rej)));
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    let batch;
    do {   // readEntries returns results in chunks, so keep asking until it is empty
      batch = await new Promise((res, rej) => reader.readEntries(res, rej));
      for (const e of batch) await readEntry(e, out);
    } while (batch.length);
  }
}
const zone = $('sec-photos');
['dragenter', 'dragover'].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add('drop'); }));
['dragleave', 'drop'].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.remove('drop'); }));
zone.addEventListener('drop', async e => {
  // read the entries right away: the browser clears them after the first await
  const entries = [...e.dataTransfer.items].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  const files = [];
  for (const en of entries) await readEntry(en, files);
  if (files.length) upload('photos', files, $('photoErr'));
});
// a file dropped outside the box would make the browser open it and leave the page
['dragover', 'drop'].forEach(ev => addEventListener(ev, e => e.preventDefault()));
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
    $('resultsHint').textContent = 'Copies are saved in workspace/' + st.session + '/results/matched. Not you in one of them? Press "Not me" under it to move it to the Rejected album.';
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
  const toMatched = which === 'rejected';
  return '<div class="pol"><a href="' + u + '" target="_blank" rel="noopener"><img loading="lazy" src="' + u + '" alt=""></a>' +
         '<button class="movebtn ' + (toMatched ? 'mint' : 'pink') + '" data-name="' + encodeURIComponent(n) +
         '" data-to="' + (toMatched ? 'matched' : 'rejected') + '" title="' +
         (toMatched ? 'Move to your photos' : 'Not you? Move to the Rejected album') + '">' +
         (toMatched ? "That's me" : 'Not me') + '</button></div>';
}

document.addEventListener('click', async e => {
  const b = e.target.closest('.movebtn');
  if (!b) return;
  b.disabled = true;
  const toRejected = b.dataset.to === 'rejected';
  const r = await api('/api/move', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({name: decodeURIComponent(b.dataset.name), to: b.dataset.to})});
  if (!r.ok) { toast(r.error || 'Could not move that photo.'); b.disabled = false; return; }
  const inOverlay = !!b.closest('#ovGrid'), panel = $('ovPanel'), top = panel.scrollTop;
  const which = toRejected ? 'matched' : 'rejected';   // the album the button was in
  await refresh();
  if (inOverlay) { await openAll(which); panel.scrollTop = top; }   // refresh the open "View all" window
  toast(toRejected
    ? 'Moved to the Rejected album. You now have ' + st.matched + (st.matched === 1 ? ' photo' : ' photos') + ' of you.'
    : 'Moved back to your photos. You now have ' + st.matched + (st.matched === 1 ? ' photo' : ' photos') + ' of you.');
});

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