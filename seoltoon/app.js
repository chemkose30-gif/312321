/* =========================================================
   썰툰 스튜디오 — 대사 + 그림 → 영상
   전부 브라우저 안에서 동작합니다 (서버/API 키 불필요).
   ========================================================= */
'use strict';

const RATIOS = {
  vertical: { w: 1080, h: 1920, label: '세로 9:16 (쇼츠·릴스)' },
  square:   { w: 1080, h: 1080, label: '정사각 1:1' },
  wide:     { w: 1920, h: 1080, label: '가로 16:9' },
};
const PALETTE = ['#222222','#ffffff','#ff6b8b','#ff9a3c','#ffd23f','#49d68a','#5cc8ff','#8b6bff','#a0522d','#8a8f9e'];
const EMOJIS = ['😀','😂','😭','😡','😱','😳','🥲','😎','❤️','💢','💦','✨','❗','❓','💡','🔥','👍','🙏','💀','🎉'];
const FONT = '"Pretendard","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const uid = () => Math.random().toString(36).slice(2, 10);

/* ---------------- 상태 ---------------- */
function newScene(patch = {}) {
  return Object.assign({
    id: uid(),
    image: null,        // dataURL
    bg: '#ffffff',
    speaker: '',
    speakerColor: '#ff6b8b',
    text: '',
    narration: '',
    captionStyle: 'bubble',
    duration: 3,
    audio: null,        // Blob
    audioDur: 0,
  }, patch);
}

let project = {
  title: '내 썰툰',
  ratio: 'vertical',
  typing: true,
  fade: true,
  fontScale: 1,
  bgm: null,            // Blob
  bgmName: '',
  bgmVolume: 0.35,
  scenes: [newScene()],
};
let current = 0;                 // 선택된 씬 index
let dirty = false;

/* ---------------- 저장 (IndexedDB) ---------------- */
const DB_NAME = 'seoltoon-studio', STORE = 'kv';
function openDB() {
  return new Promise((res, rej) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => res(req.result);
    req.onerror = () => rej(req.error);
  });
}
async function idbSet(key, val) {
  const db = await openDB();
  return new Promise((res, rej) => {
    const tx = db.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).put(val, key);
    tx.oncomplete = () => res();
    tx.onerror = () => rej(tx.error);
  });
}
async function idbGet(key) {
  const db = await openDB();
  return new Promise((res, rej) => {
    const tx = db.transaction(STORE, 'readonly');
    const r = tx.objectStore(STORE).get(key);
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}

let saveTimer = null;
function scheduleSave() {
  dirty = true;
  $('#saved').textContent = '저장 중…';
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      await idbSet('project', project);
      $('#saved').textContent = '자동 저장됨';
      dirty = false;
    } catch (e) {
      $('#saved').textContent = '저장 실패';
      console.error(e);
    }
  }, 500);
}

function toast(msg, ms = 2200) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

/* ---------------- 공통 유틸 ---------------- */
const dims = () => RATIOS[project.ratio];
const scene = () => project.scenes[current];

function sceneDuration(s) {
  const base = Number(s.duration) || 3;
  return s.audioDur ? Math.max(base, s.audioDur + 0.3) : base;
}
const totalDuration = () => project.scenes.reduce((a, s) => a + sceneDuration(s), 0);

const imgCache = new Map();
function getImage(dataURL) {
  if (!dataURL) return Promise.resolve(null);
  if (imgCache.has(dataURL)) return Promise.resolve(imgCache.get(dataURL));
  return new Promise((res) => {
    const img = new Image();
    img.onload = () => { imgCache.set(dataURL, img); res(img); };
    img.onerror = () => res(null);
    img.src = dataURL;
  });
}

async function audioDuration(blob) {
  try {
    const ab = await blob.arrayBuffer();
    const AC = window.AudioContext || window.webkitAudioContext;
    const ac = new AC();
    const buf = await ac.decodeAudioData(ab);
    ac.close();
    return buf.duration;
  } catch (e) {
    console.warn('오디오 길이 분석 실패', e);
    return 0;
  }
}

function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 8000);
}

/* ---------------- 씬 목록 ---------------- */
function renderSceneList() {
  const box = $('#scene-list');
  box.innerHTML = '';
  project.scenes.forEach((s, i) => {
    const el = document.createElement('div');
    el.className = 'scene-item' + (i === current ? ' active' : '');
    const lead = (s.speaker ? s.speaker + ': ' : '') + (s.text || s.narration || '(대사 없음)');
    el.innerHTML = `
      <img class="thumb" alt="">
      <div class="meta">
        <div class="no">씬 ${i + 1} · ${sceneDuration(s).toFixed(1)}초</div>
        <div class="lead"></div>
        <div class="row">
          <button class="btn sm" data-act="up" title="위로">▲</button>
          <button class="btn sm" data-act="down" title="아래로">▼</button>
          <button class="btn sm" data-act="dup" title="복제">⧉</button>
          <button class="btn sm danger" data-act="del" title="삭제">🗑</button>
        </div>
      </div>`;
    el.querySelector('.lead').textContent = lead;
    const thumb = el.querySelector('.thumb');
    if (s.image) thumb.src = s.image;
    if (s.audio) {
      const c = document.createElement('span');
      c.className = 'chip audio'; c.textContent = '🎤';
      el.querySelector('.row').appendChild(c);
    }
    el.addEventListener('click', (ev) => {
      const act = ev.target.dataset && ev.target.dataset.act;
      if (act) { ev.stopPropagation(); sceneAction(act, i); return; }
      selectScene(i);
    });
    box.appendChild(el);
  });
}

function sceneAction(act, i) {
  const list = project.scenes;
  if (act === 'up' && i > 0) {
    [list[i - 1], list[i]] = [list[i], list[i - 1]];
    if (current === i) current = i - 1; else if (current === i - 1) current = i;
  } else if (act === 'down' && i < list.length - 1) {
    [list[i + 1], list[i]] = [list[i], list[i + 1]];
    if (current === i) current = i + 1; else if (current === i + 1) current = i;
  } else if (act === 'dup') {
    const copy = Object.assign({}, list[i], { id: uid() });
    list.splice(i + 1, 0, copy);
    current = i + 1;
  } else if (act === 'del') {
    if (list.length === 1) { toast('마지막 씬은 지울 수 없어요'); return; }
    list.splice(i, 1);
    current = clamp(current > i ? current - 1 : current, 0, list.length - 1);
  }
  renderSceneList(); loadEditor(); loadCanvas(); scheduleSave();
}

function selectScene(i) {
  saveCanvasToScene();          // 그리던 그림 먼저 보관
  current = clamp(i, 0, project.scenes.length - 1);
  renderSceneList(); loadEditor(); loadCanvas();
}

function addScene() {
  saveCanvasToScene();
  const prev = scene();
  project.scenes.splice(current + 1, 0, newScene({
    bg: prev.bg, captionStyle: prev.captionStyle,
    speaker: prev.speaker, speakerColor: prev.speakerColor,
  }));
  current += 1;
  renderSceneList(); loadEditor(); loadCanvas(); scheduleSave();
}

/* ---------------- 편집 폼 ---------------- */
function loadEditor() {
  const s = scene();
  $('#speaker').value = s.speaker;
  $('#speaker-color').value = s.speakerColor;
  $('#text').value = s.text;
  $('#narration').value = s.narration;
  $('#caption-style').value = s.captionStyle;
  $('#duration').value = s.duration;
  $('#scene-bg').value = s.bg;
  $('#scene-badge').textContent = `씬 ${current + 1} / ${project.scenes.length}`;
  $('#audio-info').textContent = s.audio
    ? `음성 있음 · ${s.audioDur.toFixed(1)}초`
    : '녹음된 음성 없음';
}

function bindEditor() {
  const map = [
    ['#speaker', 'speaker', 'input', v => v],
    ['#speaker-color', 'speakerColor', 'input', v => v],
    ['#text', 'text', 'input', v => v],
    ['#narration', 'narration', 'input', v => v],
    ['#caption-style', 'captionStyle', 'change', v => v],
    ['#duration', 'duration', 'input', v => Math.round(clamp(Number(v) || 3, 0.5, 30) * 10) / 10],
    ['#scene-bg', 'bg', 'input', v => v],
  ];
  map.forEach(([sel, key, ev, conv]) => {
    $(sel).addEventListener(ev, (e) => {
      scene()[key] = conv(e.target.value);
      if (key === 'bg') drawBgBehind();
      renderSceneList();
      scheduleSave();
    });
  });
}

/* =========================================================
   그림판
   ========================================================= */
const dc = $('#draw-canvas');
const dctx = dc.getContext('2d', { willReadFrequently: false });
let tool = 'pen', brush = 10, color = '#222222', stampEmoji = '😀';
let drawing = false, lastPt = null, lastMid = null, touched = false;
let undoStack = [], redoStack = [];

function fitCanvasDisplay() {
  const { w, h } = dims();
  dc.style.aspectRatio = `${w} / ${h}`;
  $('#draw-size').textContent = `${w}×${h}`;
}

function snapshot() {
  try {
    undoStack.push(dc.toDataURL('image/jpeg', 0.82));
    if (undoStack.length > 20) undoStack.shift();
    redoStack.length = 0;
  } catch (e) { /* noop */ }
}

function restore(dataURL) {
  return getImage(dataURL).then((img) => {
    if (!img) return;
    dctx.clearRect(0, 0, dc.width, dc.height);
    dctx.drawImage(img, 0, 0, dc.width, dc.height);
  });
}

async function undo() {
  if (undoStack.length < 2) return;
  redoStack.push(undoStack.pop());
  await restore(undoStack[undoStack.length - 1]);
  saveCanvasToScene(true);
}
async function redo() {
  if (!redoStack.length) return;
  const d = redoStack.pop();
  undoStack.push(d);
  await restore(d);
  saveCanvasToScene(true);
}

function coverDraw(ctx, img, W, H) {
  const scale = Math.max(W / img.width, H / img.height);
  const w = img.width * scale, h = img.height * scale;
  ctx.drawImage(img, (W - w) / 2, (H - h) / 2, w, h);
}

async function loadCanvas() {
  const { w, h } = dims();
  dc.width = w; dc.height = h;
  fitCanvasDisplay();
  const s = scene();
  dctx.fillStyle = s.bg;
  dctx.fillRect(0, 0, w, h);
  if (s.image) {
    const img = await getImage(s.image);
    if (img) coverDraw(dctx, img, w, h);
  }
  touched = !!s.image;
  undoStack = []; redoStack = [];
  snapshot();
}

function drawBgBehind() {
  if (touched) return;                 // 이미 그린 그림이 있으면 건드리지 않음
  dctx.fillStyle = scene().bg;
  dctx.fillRect(0, 0, dc.width, dc.height);
  undoStack = []; snapshot();
}

function saveCanvasToScene(force) {
  if (!touched && !force) return;
  const s = scene();
  const data = dc.toDataURL('image/jpeg', 0.9);
  if (data !== s.image) {
    s.image = data;
    scheduleSave();
    renderSceneList();
  }
}
let saveCanvasTimer = null;
function scheduleCanvasSave() {
  clearTimeout(saveCanvasTimer);
  saveCanvasTimer = setTimeout(() => saveCanvasToScene(), 400);
}

function pointOf(ev) {
  const r = dc.getBoundingClientRect();
  return {
    x: (ev.clientX - r.left) / r.width * dc.width,
    y: (ev.clientY - r.top) / r.height * dc.height,
  };
}

function strokeStyleFor() {
  dctx.lineCap = 'round';
  dctx.lineJoin = 'round';
  if (tool === 'eraser') {
    dctx.globalAlpha = 1;
    dctx.strokeStyle = scene().bg;
    dctx.lineWidth = brush * 2;
  } else if (tool === 'marker') {
    dctx.globalAlpha = 0.35;
    dctx.strokeStyle = color;
    dctx.lineWidth = brush * 2.4;
  } else {
    dctx.globalAlpha = 1;
    dctx.strokeStyle = color;
    dctx.lineWidth = brush;
  }
}

function stampAt(p) {
  dctx.globalAlpha = 1;
  dctx.font = `${brush * 8}px ${FONT}`;
  dctx.textAlign = 'center';
  dctx.textBaseline = 'middle';
  dctx.fillText(stampEmoji, p.x, p.y);
}

dc.addEventListener('pointerdown', (ev) => {
  ev.preventDefault();
  dc.setPointerCapture(ev.pointerId);
  const p = pointOf(ev);
  if (tool === 'stamp') { stampAt(p); touched = true; snapshot(); scheduleCanvasSave(); return; }
  drawing = true; lastPt = p; lastMid = p;
  strokeStyleFor();
  dctx.beginPath();
  dctx.arc(p.x, p.y, dctx.lineWidth / 2, 0, Math.PI * 2);
  dctx.fillStyle = dctx.strokeStyle;
  dctx.fill();
  touched = true;
});
dc.addEventListener('pointermove', (ev) => {
  if (!drawing) return;
  const p = pointOf(ev);
  strokeStyleFor();
  const mid = { x: (lastPt.x + p.x) / 2, y: (lastPt.y + p.y) / 2 };
  dctx.beginPath();
  dctx.moveTo(lastMid.x, lastMid.y);
  dctx.quadraticCurveTo(lastPt.x, lastPt.y, mid.x, mid.y);
  dctx.stroke();
  lastPt = p; lastMid = mid;
});
['pointerup', 'pointercancel', 'pointerleave'].forEach((e) =>
  dc.addEventListener(e, () => {
    if (!drawing) return;
    drawing = false;
    if (lastMid && lastPt) {
      strokeStyleFor();
      dctx.beginPath();
      dctx.moveTo(lastMid.x, lastMid.y);
      dctx.lineTo(lastPt.x, lastPt.y);
      dctx.stroke();
    }
    dctx.globalAlpha = 1;
    snapshot();
    scheduleCanvasSave();
  })
);

/* 배경 프리셋 */
function applyPreset(kind) {
  const W = dc.width, H = dc.height;
  dctx.globalAlpha = 1;
  if (kind === 'gradient') {
    const g = dctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, color);
    g.addColorStop(1, '#ffffff');
    dctx.fillStyle = g; dctx.fillRect(0, 0, W, H);
  } else if (kind === 'speed') {
    dctx.fillStyle = '#ffffff'; dctx.fillRect(0, 0, W, H);
    const cx = W / 2, cy = H / 2, R = Math.hypot(W, H);
    dctx.strokeStyle = color;
    for (let i = 0; i < 160; i++) {
      const a = Math.random() * Math.PI * 2;
      const inner = R * (0.18 + Math.random() * 0.22);
      dctx.lineWidth = 2 + Math.random() * 10;
      dctx.beginPath();
      dctx.moveTo(cx + Math.cos(a) * inner, cy + Math.sin(a) * inner);
      dctx.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
      dctx.stroke();
    }
  } else if (kind === 'night') {
    const g = dctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, '#0b1030'); g.addColorStop(1, '#2a1b4a');
    dctx.fillStyle = g; dctx.fillRect(0, 0, W, H);
    for (let i = 0; i < 260; i++) {
      const r = Math.random() * 3 + 0.6;
      dctx.globalAlpha = 0.3 + Math.random() * 0.7;
      dctx.fillStyle = '#fff';
      dctx.beginPath();
      dctx.arc(Math.random() * W, Math.random() * H * 0.8, r, 0, Math.PI * 2);
      dctx.fill();
    }
    dctx.globalAlpha = 1;
  } else if (kind === 'note') {
    dctx.fillStyle = '#fffdf5'; dctx.fillRect(0, 0, W, H);
    dctx.strokeStyle = '#cfd8e8'; dctx.lineWidth = 2;
    for (let y = H * 0.08; y < H; y += H * 0.045) {
      dctx.beginPath(); dctx.moveTo(0, y); dctx.lineTo(W, y); dctx.stroke();
    }
  } else if (kind === 'tone') {
    dctx.fillStyle = '#ffffff'; dctx.fillRect(0, 0, W, H);
    dctx.fillStyle = color;
    const step = Math.max(10, W / 60);
    for (let y = 0; y < H; y += step) {
      for (let x = (y / step % 2) * step / 2; x < W; x += step) {
        dctx.beginPath(); dctx.arc(x, y, step * 0.18, 0, Math.PI * 2); dctx.fill();
      }
    }
  }
  touched = true; snapshot(); saveCanvasToScene();
}

function bindDrawing() {
  $$('.tool[data-tool]').forEach((b) => {
    b.addEventListener('click', () => {
      $$('.tool[data-tool]').forEach((x) => x.classList.remove('on'));
      b.classList.add('on');
      tool = b.dataset.tool;
      $('#emoji-bar').hidden = tool !== 'stamp';
    });
  });
  $('#brush').addEventListener('input', (e) => (brush = Number(e.target.value)));
  $('#color').addEventListener('input', (e) => { color = e.target.value; markSwatch(); });

  const sw = $('#swatches');
  PALETTE.forEach((c) => {
    const b = document.createElement('div');
    b.className = 'sw'; b.style.background = c; b.dataset.color = c;
    b.addEventListener('click', () => { color = c; $('#color').value = c; markSwatch(); });
    sw.appendChild(b);
  });
  markSwatch();

  const eb = $('#emoji-bar');
  EMOJIS.forEach((e, i) => {
    const b = document.createElement('button');
    b.textContent = e;
    if (i === 0) b.classList.add('on');
    b.addEventListener('click', () => {
      eb.querySelectorAll('button').forEach((x) => x.classList.remove('on'));
      b.classList.add('on');
      stampEmoji = e;
    });
    eb.appendChild(b);
  });

  $('#btn-undo').addEventListener('click', undo);
  $('#btn-redo').addEventListener('click', redo);
  $('#btn-fill').addEventListener('click', () => {
    dctx.globalAlpha = 1;
    dctx.fillStyle = color;
    dctx.fillRect(0, 0, dc.width, dc.height);
    touched = true; snapshot(); saveCanvasToScene();
  });
  $('#btn-clear').addEventListener('click', () => {
    if (!confirm('이 씬의 그림을 모두 지울까요?')) return;
    dctx.globalAlpha = 1;
    dctx.fillStyle = scene().bg;
    dctx.fillRect(0, 0, dc.width, dc.height);
    touched = false; scene().image = null;
    undoStack = []; snapshot();
    renderSceneList(); scheduleSave();
  });
  $('#preset').addEventListener('change', (e) => {
    if (!e.target.value) return;
    applyPreset(e.target.value);
    e.target.value = '';
  });
  $('#btn-upload').addEventListener('click', () => $('#file-img').click());
  $('#file-img').addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const url = await blobToDataURL(f);
    const img = await getImage(url);
    if (img) { coverDraw(dctx, img, dc.width, dc.height); touched = true; snapshot(); saveCanvasToScene(); }
    e.target.value = '';
  });

  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
      if (document.activeElement && /INPUT|TEXTAREA/.test(document.activeElement.tagName)) return;
      e.preventDefault();
      e.shiftKey ? redo() : undo();
    }
  });
}
function markSwatch() {
  $$('.sw').forEach((s) => s.classList.toggle('on', s.dataset.color.toLowerCase() === color.toLowerCase()));
}

function blobToDataURL(blob) {
  return new Promise((res) => {
    const fr = new FileReader();
    fr.onload = () => res(fr.result);
    fr.readAsDataURL(blob);
  });
}

/* =========================================================
   프레임 렌더링 (그림 + 대사 합성)
   ========================================================= */
function roundRect(ctx, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

function wrapText(ctx, text, maxWidth) {
  const out = [];
  text.split('\n').forEach((para) => {
    if (!para) { out.push(''); return; }
    let line = '';
    for (const token of para.split(/(\s+)/)) {
      if (!token) continue;
      if (ctx.measureText(line + token).width <= maxWidth) { line += token; continue; }
      if (ctx.measureText(token).width > maxWidth) {      // 긴 단어/한글 덩어리는 글자 단위로
        for (const ch of token) {
          if (ctx.measureText(line + ch).width > maxWidth) { out.push(line.trimEnd()); line = ch; }
          else line += ch;
        }
      } else {
        out.push(line.trimEnd());
        line = token.trimStart();
      }
    }
    out.push(line.trimEnd());
  });
  return out.filter((l, i, a) => !(l === '' && (i === 0 || i === a.length - 1)));
}

function revealText(text, local, dur) {
  if (!project.typing || !text) return text;
  const typeDur = clamp(text.length * 0.055, 0.5, Math.max(0.6, dur * 0.55));
  const p = clamp((local - 0.15) / typeDur, 0, 1);
  return text.slice(0, Math.ceil(text.length * p));
}

function drawFrame(ctx, s, local, dur, img) {
  const W = ctx.canvas.width, H = ctx.canvas.height;
  const fs = project.fontScale;

  ctx.save();
  ctx.globalAlpha = 1;
  ctx.fillStyle = s.bg || '#ffffff';
  ctx.fillRect(0, 0, W, H);
  if (img) coverDraw(ctx, img, W, H);

  const S = Math.min(W, H);                 // 비율이 달라도 글자 크기가 비슷하게 보이도록
  const pad = S * 0.055;
  const base = S * 0.047 * fs;

  /* --- 나레이션 (상단) --- */
  if (s.narration && s.narration.trim()) {
    const f = base * 0.88;
    ctx.font = `600 ${f}px ${FONT}`;
    const maxW = Math.min(W - pad * 2 - f * 1.2, S * 1.6);
    const lines = wrapText(ctx, s.narration.trim(), maxW);
    const lh = f * 1.45;
    const boxH = lines.length * lh + f * 1.1;
    const boxY = pad;
    const boxW = Math.min(W - pad * 2, maxW + f * 1.2);
    const boxX = (W - boxW) / 2;
    ctx.fillStyle = 'rgba(14,15,20,.78)';
    roundRect(ctx, boxX, boxY, boxW, boxH, f * 0.55);
    ctx.fill();
    ctx.fillStyle = '#f2f4fb';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    lines.forEach((ln, i) => ctx.fillText(ln, boxX + f * 0.6, boxY + f * 0.55 + i * lh));
  }

  /* --- 대사 --- */
  const raw = (s.text || '').trim();
  if (raw && s.captionStyle !== 'none') {
    const shown = revealText(raw, local, dur);
    const f = base;
    const lh = f * 1.45;

    if (s.captionStyle === 'subtitle') {
      ctx.font = `700 ${f}px ${FONT}`;
      const maxW = Math.min(W - pad * 2, S * 1.7);
      const lines = wrapText(ctx, shown || ' ', maxW);
      const allLines = wrapText(ctx, raw, maxW);           // 높이는 전체 기준으로 고정
      const nameH = s.speaker ? f * 1.35 : 0;
      const boxH = allLines.length * lh + f * 1.0 + nameH;
      const boxY = H - pad - boxH;
      const barW = Math.min(W - pad * 1.2, maxW + pad * 1.2);
      ctx.fillStyle = 'rgba(8,9,14,.72)';
      roundRect(ctx, (W - barW) / 2, boxY, barW, boxH, f * 0.45);
      ctx.fill();
      let y = boxY + f * 0.5;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      if (s.speaker) {
        ctx.fillStyle = s.speakerColor || '#ff6b8b';
        ctx.font = `800 ${f * 0.82}px ${FONT}`;
        ctx.fillText(s.speaker, W / 2, y);
        y += nameH;
      }
      ctx.fillStyle = '#ffffff';
      ctx.font = `700 ${f}px ${FONT}`;
      lines.forEach((ln, i) => ctx.fillText(ln, W / 2, y + i * lh));
    } else {
      /* 말풍선 */
      ctx.font = `700 ${f}px ${FONT}`;
      const inner = f * 0.85;
      const maxW = Math.min(W - pad * 2 - inner * 2, S * 1.55);
      const allLines = wrapText(ctx, raw, maxW);
      const lines = wrapText(ctx, shown || ' ', maxW);
      const boxW = Math.min(
        W - pad * 2,
        Math.max(S * 0.5, Math.max(...allLines.map((l) => ctx.measureText(l).width)) + inner * 2)
      );
      const boxH = allLines.length * lh + inner * 1.6;
      const boxX = (W - boxW) / 2;
      const boxY = H - pad - boxH - H * 0.035;

      ctx.save();
      ctx.shadowColor = 'rgba(0,0,0,.35)';
      ctx.shadowBlur = f * 0.8;
      ctx.shadowOffsetY = f * 0.2;
      ctx.fillStyle = '#ffffff';
      roundRect(ctx, boxX, boxY, boxW, boxH, f * 0.8);
      ctx.fill();
      ctx.restore();

      // 꼬리
      ctx.fillStyle = '#ffffff';
      ctx.beginPath();
      const tx = boxX + boxW * 0.26;
      ctx.moveTo(tx, boxY + 2);
      ctx.lineTo(tx + f * 0.9, boxY + 2);
      ctx.lineTo(tx - f * 0.1, boxY - f * 0.95);
      ctx.closePath();
      ctx.fill();

      ctx.lineWidth = Math.max(3, f * 0.11);
      ctx.strokeStyle = '#151720';
      roundRect(ctx, boxX, boxY, boxW, boxH, f * 0.8);
      ctx.stroke();

      // 화자 이름표
      if (s.speaker) {
        const nf = f * 0.72;
        ctx.font = `800 ${nf}px ${FONT}`;
        const tw = ctx.measureText(s.speaker).width;
        const nw = tw + nf * 1.2, nh = nf * 1.9;
        const nx = boxX + f * 0.4, ny = boxY - nh - f * 0.35;
        ctx.fillStyle = s.speakerColor || '#ff6b8b';
        roundRect(ctx, nx, ny, nw, nh, nh / 2);
        ctx.fill();
        ctx.fillStyle = '#ffffff';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(s.speaker, nx + nw / 2, ny + nh / 2 + nf * 0.05);
      }

      ctx.fillStyle = '#16181f';
      ctx.font = `700 ${f}px ${FONT}`;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      lines.forEach((ln, i) => ctx.fillText(ln, boxX + inner, boxY + inner * 0.8 + i * lh));
    }
  }

  /* --- 페이드 --- */
  if (project.fade) {
    const a = Math.min(clamp(local / 0.28, 0, 1), clamp((dur - local) / 0.28, 0, 1));
    if (a < 1) {
      ctx.fillStyle = `rgba(0,0,0,${1 - a})`;
      ctx.fillRect(0, 0, W, H);
    }
  }
  ctx.restore();
}

/* =========================================================
   타임라인 재생 / 녹화
   ========================================================= */
let running = false, stopRequested = false;

async function decodeAll(ac) {
  const buffers = await Promise.all(project.scenes.map(async (s) => {
    if (!s.audio) return null;
    try { return await ac.decodeAudioData(await s.audio.arrayBuffer()); }
    catch (e) { return null; }
  }));
  let bgm = null;
  if (project.bgm) {
    try { bgm = await ac.decodeAudioData(await project.bgm.arrayBuffer()); } catch (e) { bgm = null; }
  }
  return { buffers, bgm };
}

/**
 * 씬들을 실시간으로 그려주는 공통 루프.
 * @param ctx 그릴 캔버스 컨텍스트 (크기는 비율과 동일해야 함)
 * @param opts {record:boolean, onProgress(p, idx)}
 */
async function runTimeline(ctx, opts = {}) {
  const AC = window.AudioContext || window.webkitAudioContext;
  const ac = new AC();
  await ac.resume();

  const outputs = [ac.destination];
  let streamDest = null;
  if (opts.record) {
    streamDest = ac.createMediaStreamDestination();
    outputs.push(streamDest);
  }
  const master = ac.createGain();
  master.gain.value = 1;
  outputs.forEach((o) => master.connect(o));

  const images = await Promise.all(project.scenes.map((s) => getImage(s.image)));
  const { buffers, bgm } = await decodeAll(ac);

  const durs = project.scenes.map(sceneDuration);
  const starts = [];
  let acc = 0;
  durs.forEach((d) => { starts.push(acc); acc += d; });
  const total = acc;

  const t0 = ac.currentTime + 0.25;
  const sources = [];

  buffers.forEach((buf, i) => {
    if (!buf) return;
    const src = ac.createBufferSource();
    src.buffer = buf;
    src.connect(master);
    src.start(t0 + starts[i]);
    sources.push(src);
  });
  if (bgm) {
    const src = ac.createBufferSource();
    src.buffer = bgm; src.loop = true;
    const g = ac.createGain();
    g.gain.value = project.bgmVolume;
    src.connect(g); g.connect(master);
    src.start(t0);
    src.stop(t0 + total);
    sources.push(src);
  }

  if (opts.onReady) opts.onReady({ streamDest, total });

  return new Promise((resolve) => {
    const step = () => {
      const T = ac.currentTime - t0;
      const t = clamp(T, 0, total);
      let idx = 0;
      while (idx < durs.length - 1 && t >= starts[idx] + durs[idx]) idx++;
      drawFrame(ctx, project.scenes[idx], t - starts[idx], durs[idx], images[idx]);
      if (opts.onProgress) opts.onProgress(clamp(T / total, 0, 1), idx);

      if (stopRequested || T >= total) {
        sources.forEach((s) => { try { s.stop(); } catch (e) {} });
        setTimeout(() => ac.close().catch(() => {}), 300);
        resolve();
        return;
      }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}

function openPlayModal(title) {
  $('#play-title').textContent = title;
  $('#result').innerHTML = '';
  $('#play-bar').style.width = '0%';
  $('#modal-play').hidden = false;
  const { w, h } = dims();
  const stage = $('#stage');
  stage.width = w; stage.height = h;
  stage.style.aspectRatio = `${w} / ${h}`;
  return stage.getContext('2d');
}
function closePlayModal() {
  stopRequested = true;
  $('#modal-play').hidden = true;
}

async function preview() {
  if (running) return;
  saveCanvasToScene();
  const ctx = openPlayModal('미리보기');
  running = true; stopRequested = false;
  $('#play-status').textContent = '재생 중…';
  try {
    await runTimeline(ctx, {
      onProgress: (p, i) => {
        $('#play-bar').style.width = (p * 100).toFixed(1) + '%';
        $('#play-status').textContent = `재생 중 · 씬 ${i + 1}/${project.scenes.length}`;
      },
    });
    $('#play-status').textContent = stopRequested ? '중지됨' : '재생 완료';
  } finally { running = false; }
}

function pickMime() {
  const cands = [
    'video/webm;codecs=vp9,opus',
    'video/webm;codecs=vp8,opus',
    'video/webm',
    'video/mp4;codecs=avc1.42E01E,mp4a.40.2',
    'video/mp4',
  ];
  if (!window.MediaRecorder) return '';
  for (const m of cands) { if (MediaRecorder.isTypeSupported(m)) return m; }
  return '';
}

async function exportVideo() {
  if (running) return;
  if (!window.MediaRecorder) { alert('이 브라우저는 영상 녹화를 지원하지 않아요. 크롬/엣지 최신 버전을 사용해 주세요.'); return; }
  saveCanvasToScene();
  const mime = pickMime();
  const stage = $('#stage');
  const ctx = openPlayModal('영상 만드는 중');
  running = true; stopRequested = false;

  const total = totalDuration();
  $('#play-status').textContent = `녹화 준비 중… (총 ${total.toFixed(1)}초, 실시간으로 진행됩니다)`;

  const stream = stage.captureStream(30);
  let recorder = null;
  const chunks = [];

  try {
    await runTimeline(ctx, {
      record: true,
      onReady: ({ streamDest }) => {
        streamDest.stream.getAudioTracks().forEach((t) => stream.addTrack(t));
        recorder = new MediaRecorder(stream, mime
          ? { mimeType: mime, videoBitsPerSecond: 6_000_000 }
          : { videoBitsPerSecond: 6_000_000 });
        recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
        recorder.start(200);
      },
      onProgress: (p, i) => {
        $('#play-bar').style.width = (p * 100).toFixed(1) + '%';
        $('#play-status').textContent = `녹화 중 ${(p * 100).toFixed(0)}% · 씬 ${i + 1}/${project.scenes.length}`;
      },
    });

    if (recorder && recorder.state !== 'inactive') {
      await new Promise((res) => { recorder.onstop = res; recorder.stop(); });
    }
    const type = (recorder && recorder.mimeType) || mime || 'video/webm';
    const blob = new Blob(chunks, { type });
    const ext = type.includes('mp4') ? 'mp4' : 'webm';
    const name = `${(project.title || '썰툰').replace(/[\\/:*?"<>|]/g, '_')}.${ext}`;

    $('#play-status').textContent = `완료! ${(blob.size / 1048576).toFixed(1)}MB · ${ext.toUpperCase()}`;
    const box = $('#result');
    box.innerHTML = '';
    const dl = document.createElement('button');
    dl.className = 'btn primary';
    dl.textContent = '⬇ 영상 내려받기';
    dl.onclick = () => download(blob, name);
    box.appendChild(dl);
    const hint = document.createElement('span');
    hint.className = 'muted';
    hint.style.marginLeft = '10px';
    hint.textContent = ext === 'webm'
      ? 'WebM 파일입니다 — 유튜브/인스타 업로드는 그대로 가능해요.'
      : '';
    box.appendChild(hint);
    download(blob, name);
  } catch (e) {
    console.error(e);
    $('#play-status').textContent = '녹화 실패: ' + e.message;
  } finally { running = false; }
}

/* =========================================================
   음성 (마이크 녹음 · 파일 · TTS 미리듣기)
   ========================================================= */
let micRec = null, micStream = null, micChunks = [];

async function toggleRecord() {
  const btn = $('#btn-rec');
  if (micRec && micRec.state === 'recording') {
    micRec.stop();
    return;
  }
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    alert('마이크를 사용할 수 없어요. 브라우저 권한을 확인해 주세요.');
    return;
  }
  micChunks = [];
  const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'].find(
    (m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)
  );
  micRec = new MediaRecorder(micStream, mime ? { mimeType: mime } : undefined);
  micRec.ondataavailable = (e) => { if (e.data && e.data.size) micChunks.push(e.data); };
  micRec.onstop = async () => {
    micStream.getTracks().forEach((t) => t.stop());
    btn.innerHTML = '🎤 녹음';
    const blob = new Blob(micChunks, { type: micRec.mimeType || 'audio/webm' });
    const s = scene();
    s.audio = blob;
    s.audioDur = await audioDuration(blob);
    loadEditor(); renderSceneList(); scheduleSave();
    toast(`녹음 완료 · ${s.audioDur.toFixed(1)}초`);
  };
  micRec.start();
  btn.innerHTML = '<i class="rec-dot"></i>정지';
  toast('녹음 중… 다시 누르면 정지');
}

function bindAudio() {
  $('#btn-rec').addEventListener('click', toggleRecord);
  $('#btn-audio-up').addEventListener('click', () => $('#file-audio').click());
  $('#file-audio').addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const s = scene();
    s.audio = f;
    s.audioDur = await audioDuration(f);
    loadEditor(); renderSceneList(); scheduleSave();
    e.target.value = '';
  });
  $('#btn-audio-play').addEventListener('click', () => {
    const s = scene();
    if (!s.audio) { toast('이 씬에는 음성이 없어요'); return; }
    const a = new Audio(URL.createObjectURL(s.audio));
    a.play();
  });
  $('#btn-audio-del').addEventListener('click', () => {
    const s = scene();
    s.audio = null; s.audioDur = 0;
    loadEditor(); renderSceneList(); scheduleSave();
  });
  $('#btn-tts').addEventListener('click', () => {
    const s = scene();
    const txt = [s.narration, s.text].filter(Boolean).join('. ');
    if (!txt) { toast('읽을 대사가 없어요'); return; }
    if (!window.speechSynthesis) { toast('이 브라우저는 TTS를 지원하지 않아요'); return; }
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(txt);
    u.lang = 'ko-KR';
    const ko = speechSynthesis.getVoices().find((v) => /ko/i.test(v.lang));
    if (ko) u.voice = ko;
    speechSynthesis.speak(u);
    toast('TTS는 미리듣기 전용이에요. 영상에 목소리를 넣으려면 🎤 녹음을 사용하세요.', 3600);
  });
}

/* =========================================================
   대본 일괄 입력
   ========================================================= */
const SPEAKER_COLORS = ['#ff6b8b', '#5cc8ff', '#49d68a', '#ffc65c', '#a78bfa', '#ff9a6b'];
const speakerColorMap = new Map();
function colorForSpeaker(name) {
  if (!speakerColorMap.has(name)) {
    speakerColorMap.set(name, SPEAKER_COLORS[speakerColorMap.size % SPEAKER_COLORS.length]);
  }
  return speakerColorMap.get(name);
}
const estDur = (t) => Math.round(clamp(1.2 + t.length * 0.13, 1.8, 9) * 10) / 10;

function parseScript(text) {
  return text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean).map((line) => {
    if (line.startsWith('#')) {
      const n = line.replace(/^#+\s*/, '');
      return newScene({ narration: n, captionStyle: 'none', duration: estDur(n), bg: '#0f1118' });
    }
    const m = line.match(/^([^:：]{1,14})\s*[:：]\s*(.+)$/);
    if (m) {
      const name = m[1].trim();
      return newScene({
        speaker: name, speakerColor: colorForSpeaker(name),
        text: m[2].trim(), duration: estDur(m[2]),
      });
    }
    return newScene({ text: line, duration: estDur(line) });
  });
}

function bindBulk() {
  $('#btn-bulk').addEventListener('click', () => {
    $('#modal-bulk').hidden = false;
    $('#bulk-text').focus();
  });
  $('#btn-bulk-cancel').addEventListener('click', () => ($('#modal-bulk').hidden = true));
  $('#btn-bulk-ok').addEventListener('click', () => {
    const scenes = parseScript($('#bulk-text').value);
    if (!scenes.length) { toast('내용을 입력해 주세요'); return; }
    saveCanvasToScene();
    if ($('#bulk-replace').checked) {
      project.scenes = scenes;
      current = 0;
    } else {
      project.scenes = project.scenes.concat(scenes);
      current = project.scenes.length - scenes.length;
    }
    $('#modal-bulk').hidden = true;
    renderSceneList(); loadEditor(); loadCanvas(); scheduleSave();
    toast(`${scenes.length}개 씬을 만들었어요`);
  });
}

/* =========================================================
   프로젝트 저장 / 불러오기
   ========================================================= */
async function exportJSON() {
  saveCanvasToScene();
  const out = {
    version: 1,
    title: project.title, ratio: project.ratio,
    typing: project.typing, fade: project.fade, fontScale: project.fontScale,
    bgmVolume: project.bgmVolume, bgmName: project.bgmName,
    bgm: project.bgm ? await blobToDataURL(project.bgm) : null,
    scenes: await Promise.all(project.scenes.map(async (s) => Object.assign({}, s, {
      audio: s.audio ? await blobToDataURL(s.audio) : null,
    }))),
  };
  download(new Blob([JSON.stringify(out)], { type: 'application/json' }),
    `${(project.title || '썰툰').replace(/[\\/:*?"<>|]/g, '_')}.seoltoon.json`);
  toast('프로젝트 파일을 저장했어요');
}

async function dataURLtoBlob(d) {
  if (!d) return null;
  const res = await fetch(d);
  return await res.blob();
}

async function importJSON(file) {
  const data = JSON.parse(await file.text());
  project.title = data.title || '내 썰툰';
  project.ratio = RATIOS[data.ratio] ? data.ratio : 'vertical';
  project.typing = data.typing !== false;
  project.fade = data.fade !== false;
  project.fontScale = data.fontScale || 1;
  project.bgmVolume = data.bgmVolume != null ? data.bgmVolume : 0.35;
  project.bgmName = data.bgmName || '';
  project.bgm = await dataURLtoBlob(data.bgm);
  project.scenes = [];
  for (const s of (data.scenes || [])) {
    project.scenes.push(newScene(Object.assign({}, s, { audio: await dataURLtoBlob(s.audio) })));
  }
  if (!project.scenes.length) project.scenes = [newScene()];
  current = 0;
  applyProjectToUI();
  renderSceneList(); loadEditor(); loadCanvas(); scheduleSave();
  toast('불러왔어요');
}

/* =========================================================
   초기화 / 바인딩
   ========================================================= */
function applyProjectToUI() {
  $('#proj-title').value = project.title;
  $('#ratio').value = project.ratio;
  $('#opt-typing').checked = project.typing;
  $('#opt-fade').checked = project.fade;
  $('#font-scale').value = Math.round(project.fontScale * 100);
  $('#font-label').textContent = Math.round(project.fontScale * 100) + '%';
  $('#bgm-volume').value = Math.round(project.bgmVolume * 100);
  $('#bgm-vol-label').textContent = Math.round(project.bgmVolume * 100) + '%';
  $('#bgm-info').textContent = project.bgm ? (project.bgmName || 'BGM 있음') : '없음';
}

function bindGlobal() {
  const rs = $('#ratio');
  Object.entries(RATIOS).forEach(([k, v]) => {
    const o = document.createElement('option');
    o.value = k; o.textContent = v.label;
    rs.appendChild(o);
  });

  $('#proj-title').addEventListener('input', (e) => { project.title = e.target.value; scheduleSave(); });
  rs.addEventListener('change', (e) => {
    saveCanvasToScene();
    project.ratio = e.target.value;
    loadCanvas(); scheduleSave();
  });

  $('#btn-add').addEventListener('click', addScene);
  $('#btn-preview').addEventListener('click', preview);
  $('#btn-export').addEventListener('click', exportVideo);
  $('#btn-stop').addEventListener('click', () => { stopRequested = true; });
  $('#btn-close-play').addEventListener('click', closePlayModal);
  $('#btn-menu').addEventListener('click', () => {
    const d = document.querySelector('details.settings');
    d.open = !d.open;
    if (d.open) d.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });

  $('#opt-typing').addEventListener('change', (e) => { project.typing = e.target.checked; scheduleSave(); });
  $('#opt-fade').addEventListener('change', (e) => { project.fade = e.target.checked; scheduleSave(); });
  $('#font-scale').addEventListener('input', (e) => {
    project.fontScale = Number(e.target.value) / 100;
    $('#font-label').textContent = e.target.value + '%';
    scheduleSave();
  });
  $('#bgm-volume').addEventListener('input', (e) => {
    project.bgmVolume = Number(e.target.value) / 100;
    $('#bgm-vol-label').textContent = e.target.value + '%';
    scheduleSave();
  });
  $('#btn-bgm').addEventListener('click', () => $('#file-bgm').click());
  $('#file-bgm').addEventListener('change', (e) => {
    const f = e.target.files[0];
    if (!f) return;
    project.bgm = f; project.bgmName = f.name;
    $('#bgm-info').textContent = f.name;
    scheduleSave();
    e.target.value = '';
  });
  $('#btn-bgm-del').addEventListener('click', () => {
    project.bgm = null; project.bgmName = '';
    $('#bgm-info').textContent = '없음';
    scheduleSave();
  });

  $('#btn-save-json').addEventListener('click', exportJSON);
  $('#btn-load-json').addEventListener('click', () => $('#file-json').click());
  $('#file-json').addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if (f) { try { await importJSON(f); } catch (err) { alert('불러오기 실패: ' + err.message); } }
    e.target.value = '';
  });
  $('#btn-png').addEventListener('click', async () => {
    saveCanvasToScene();
    const { w, h } = dims();
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    const s = scene();
    const saveFade = project.fade;
    project.fade = false;
    drawFrame(c.getContext('2d'), s, 99, 100, await getImage(s.image));
    project.fade = saveFade;
    c.toBlob((b) => download(b, `${project.title || '썰툰'}_씬${current + 1}.png`), 'image/png');
  });
  $('#btn-reset').addEventListener('click', async () => {
    if (!confirm('모든 씬과 그림을 지우고 처음부터 시작할까요?')) return;
    project.scenes = [newScene()];
    project.title = '내 썰툰';
    project.bgm = null; project.bgmName = '';
    current = 0;
    applyProjectToUI(); renderSceneList(); loadEditor(); loadCanvas(); scheduleSave();
  });

  window.addEventListener('beforeunload', () => { saveCanvasToScene(); });
}

async function init() {
  bindGlobal();
  bindEditor();
  bindDrawing();
  bindAudio();
  bindBulk();

  try {
    const saved = await idbGet('project');
    if (saved && Array.isArray(saved.scenes) && saved.scenes.length) {
      project = Object.assign(project, saved);
      project.scenes = saved.scenes.map((s) => newScene(s));
    }
  } catch (e) { console.warn('저장된 프로젝트 불러오기 실패', e); }

  applyProjectToUI();
  renderSceneList();
  loadEditor();
  await loadCanvas();

  if (window.speechSynthesis) speechSynthesis.getVoices();
}

init();
