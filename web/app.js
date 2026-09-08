/* Lyric overlay — runtime.
 *
 * Python pushes discrete state (track, lyrics, a position sync every 250ms).
 * This layer runs its own clock between syncs, so the highlight moves at true
 * frame rate rather than stepping four times a second.
 *
 * The word sweep is *scrubbed*, not animated: its position is derived from the
 * playhead every frame. Handing it to an animation library with a duration
 * would desynchronise it the moment the user seeks, pauses, or the track drifts.
 * Motion is used for the things that genuinely are animations — the card
 * entrance and the line-to-line scroll, both of which can be interrupted.
 */

const { animate } = window.Motion;

const el = {
  card: document.getElementById('card'),
  title: document.getElementById('title'),
  artist: document.getElementById('artist'),
  lines: document.getElementById('lines'),
  stage: document.getElementById('stage'),
  notice: document.getElementById('notice'),
  progressFill: document.getElementById('progressFill'),
};

const state = {
  lines: [],
  synced: false,
  hasLyrics: false,
  durationMs: 0,

  // Playhead, kept as an anchor plus a local clock reading.
  anchorMs: 0,
  anchorAt: performance.now(),
  playing: false,

  activeIndex: -1,
  lineEls: [],
  wordEls: [],   // parallel to lineEls: array of arrays
  lastP: [],     // last written --p per word, to skip redundant style writes
};

/* ------------------------------------------------------------------ bridge */

function send(message) {
  // Picked up by BridgePage.javaScriptConsoleMessage on the Python side.
  console.log('LYRICBRIDGE' + JSON.stringify(message));
}

/* ------------------------------------------------------------ playhead ---- */

function positionMs() {
  if (!state.playing) return state.anchorMs;
  return state.anchorMs + (performance.now() - state.anchorAt);
}

/* -------------------------------------------------------------- rendering - */

function clearLines() {
  el.lines.replaceChildren();
  state.lineEls = [];
  state.wordEls = [];
  state.lastP = [];
  state.activeIndex = -1;
}

function buildLines() {
  const frag = document.createDocumentFragment();

  state.lines.forEach((line, index) => {
    const lineEl = document.createElement('div');
    lineEl.className = 'line';
    lineEl.dataset.role = 'far';

    const words = [];
    const progress = [];

    if (line.instrumental) {
      lineEl.dataset.instrumental = '1';
    } else if (state.synced && line.words.length) {
      line.words.forEach((word, wordIndex) => {
        const span = document.createElement('span');
        span.className = 'word';
        // Trailing space lives inside the span (white-space: pre) so the sweep
        // carries through the gap instead of stalling between words.
        span.textContent =
          wordIndex < line.words.length - 1 ? word.t + ' ' : word.t;
        lineEl.appendChild(span);
        words.push(span);
        progress.push(-1);
      });
    } else {
      lineEl.dataset.unsynced = '1';
      lineEl.textContent = line.text;
    }

    frag.appendChild(lineEl);
    state.lineEls.push(lineEl);
    state.wordEls.push(words);
    state.lastP.push(progress);
  });

  el.lines.appendChild(frag);
}

/** Index of the line containing `ms`, or -1 before the first line. */
function findLineIndex(ms) {
  const lines = state.lines;
  if (!lines.length || ms < lines[0].start) return -1;

  let low = 0;
  let high = lines.length - 1;
  let found = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (lines[mid].start <= ms) {
      found = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return found;
}

function setActiveLine(index) {
  if (index === state.activeIndex) return;

  const previous = state.activeIndex;
  state.activeIndex = index;

  // Reset the line we are leaving so a re-entry (from a seek) re-sweeps.
  if (previous >= 0 && state.wordEls[previous]) {
    const words = state.wordEls[previous];
    for (let i = 0; i < words.length; i++) {
      words[i].style.setProperty('--p', '1');
      words[i].removeAttribute('data-live');
      state.lastP[previous][i] = 1;
    }
  }

  for (let i = 0; i < state.lineEls.length; i++) {
    const distance = Math.abs(i - index);
    const lineEl = state.lineEls[i];
    if (i === index) lineEl.dataset.role = 'active';
    else if (distance === 1) lineEl.dataset.role = 'near';
    else lineEl.dataset.role = 'far';
  }

  // Words ahead of the playhead start unsung.
  if (index >= 0 && state.wordEls[index]) {
    const words = state.wordEls[index];
    for (let i = 0; i < words.length; i++) {
      words[i].style.setProperty('--p', '0');
      state.lastP[index][i] = 0;
    }
  }

  scrollToActive(index);
  updateCardState();
}

function scrollToActive(index) {
  if (index < 0) {
    animate(el.lines, { y: 0 }, { type: 'spring', bounce: 0.15, visualDuration: 0.4 });
    return;
  }
  const lineEl = state.lineEls[index];
  if (!lineEl) return;

  // Centre the active line on the stage's midline. offsetTop is relative to
  // .lines, which is itself pinned at 50% of the stage.
  const target = -(lineEl.offsetTop + lineEl.offsetHeight / 2);
  animate(el.lines, { y: target }, { type: 'spring', bounce: 0.16, visualDuration: 0.42 });
}

function updateCardState() {
  const index = state.activeIndex;

  if (!state.hasLyrics) {
    el.card.dataset.state = state.lines.length ? 'nolyrics' : 'idle';
    return;
  }
  if (index >= 0 && state.lines[index] && state.lines[index].instrumental) {
    el.card.dataset.state = 'instrumental';
    return;
  }
  // Before the first lyric of a track, breathe rather than show a blank card.
  el.card.dataset.state = index < 0 ? 'instrumental' : 'playing';
}

/* ------------------------------------------------------------- frame loop - */

function tick() {
  const ms = positionMs();

  if (state.durationMs > 0) {
    const ratio = Math.max(0, Math.min(1, ms / state.durationMs));
    el.progressFill.style.width = (ratio * 100).toFixed(3) + '%';
  }

  if (state.hasLyrics && state.synced) {
    const index = findLineIndex(ms);
    if (index !== state.activeIndex) setActiveLine(index);

    if (index >= 0) {
      const words = state.lines[index].words;
      const spans = state.wordEls[index];
      const cache = state.lastP[index];

      for (let i = 0; i < spans.length; i++) {
        const word = words[i];
        const span = word.e > word.s ? (ms - word.s) / (word.e - word.s) : (ms >= word.s ? 1 : 0);
        const p = span < 0 ? 0 : span > 1 ? 1 : span;

        // Skip sub-perceptual updates; each write invalidates a paint.
        if (Math.abs(p - cache[i]) > 0.008) {
          spans[i].style.setProperty('--p', p.toFixed(3));
          cache[i] = p;
        }

        const live = p > 0 && p < 1;
        if (live && spans[i].dataset.live !== '1') spans[i].dataset.live = '1';
        else if (!live && spans[i].dataset.live === '1') spans[i].removeAttribute('data-live');
      }
    }
  }

  requestAnimationFrame(tick);
}

/* ---------------------------------------------------------- python → js --- */

window.overlaySetTrack = function (payload) {
  el.title.textContent = payload.title || '';
  el.artist.textContent = payload.artist || '';
  state.durationMs = payload.durationMs || 0;

  // Clear immediately: showing the previous track's lyrics while the new
  // track's fetch is in flight is worse than showing nothing.
  state.lines = [];
  state.hasLyrics = false;
  state.synced = false;
  clearLines();
  el.card.dataset.state = 'instrumental';
  animate(el.lines, { y: 0 }, { duration: 0 });
};

window.overlaySetLyrics = function (payload) {
  state.lines = payload.lines || [];
  state.synced = !!payload.synced;
  state.hasLyrics = state.lines.length > 0 && state.synced;

  clearLines();

  if (!state.lines.length) {
    el.notice.textContent = 'no lyrics found';
    el.card.dataset.state = 'nolyrics';
    return;
  }

  buildLines();

  if (!state.synced) {
    el.notice.textContent = 'lyrics found, but not time-synced';
    el.card.dataset.state = 'nolyrics';
    // Show the opening lines so the card is not empty.
    state.lineEls.slice(0, 3).forEach((node, i) => {
      node.dataset.role = i === 0 ? 'active' : 'near';
    });
    return;
  }

  el.notice.textContent = '';
  setActiveLine(findLineIndex(positionMs()));
};

window.overlaySync = function (payload) {
  state.anchorMs = payload.positionMs || 0;
  state.anchorAt = performance.now();
  state.playing = !!payload.playing;
};

window.overlaySetTheme = function (payload) {
  const root = document.documentElement.style;
  // Both are registered with @property, so simply assigning them crossfades
  // via the transition on :root rather than snapping.
  if (payload.plate) root.setProperty('--plate-tint', payload.plate);
  if (payload.accent) root.setProperty('--accent', payload.accent);
  el.card.dataset.themed = payload.colorful ? '1' : '0';
};

window.overlaySetAppearance = function (payload) {
  const root = document.documentElement.style;
  if (typeof payload.plateCore === 'number') {
    root.setProperty('--plate-core', payload.plateCore.toFixed(3));
  }
  if (typeof payload.plateEdge === 'number') {
    root.setProperty('--plate-edge', payload.plateEdge.toFixed(3));
  }
  if (typeof payload.textScale === 'number') {
    textScale = payload.textScale;
    scaleType();
    scrollToActive(state.activeIndex);
  }
};

window.overlaySetIdle = function () {
  el.title.textContent = 'Lyric Overlay';
  el.artist.textContent = 'waiting for Spotify';
  state.lines = [];
  state.hasLyrics = false;
  state.playing = false;
  state.durationMs = 0;
  clearLines();
  el.progressFill.style.width = '0%';
  el.card.dataset.state = 'idle';
};

/* -------------------------------------------------------------- controls -- */

/* The window is frameless, so it has no OS resize border. The outer few pixels
   of the card stand in for one: pointing there switches the cursor and starts a
   resize instead of a move. */
const EDGE = 9;

const CURSORS = {
  n: 'ns-resize', s: 'ns-resize',
  e: 'ew-resize', w: 'ew-resize',
  ne: 'nesw-resize', sw: 'nesw-resize',
  nw: 'nwse-resize', se: 'nwse-resize',
};

function edgeAt(event) {
  const r = el.card.getBoundingClientRect();
  let edge = '';
  if (event.clientY - r.top <= EDGE) edge += 'n';
  else if (r.bottom - event.clientY <= EDGE) edge += 's';
  if (event.clientX - r.left <= EDGE) edge += 'w';
  else if (r.right - event.clientX <= EDGE) edge += 'e';
  return edge;
}

let gesture = null; // 'move' | 'resize' | null

el.card.addEventListener('pointermove', (event) => {
  // While a gesture is running Python owns the window; leave the cursor alone
  // so it does not flicker as the edges move under the pointer.
  if (gesture) return;
  if (event.target.closest('[data-no-drag]')) {
    el.card.style.cursor = '';
    return;
  }
  const edge = edgeAt(event);
  el.card.style.cursor = edge ? CURSORS[edge] : '';
});

el.card.addEventListener('pointerleave', () => {
  if (!gesture) el.card.style.cursor = '';
});

el.card.addEventListener('pointerdown', (event) => {
  if (event.button !== 0) return;
  if (event.target.closest('[data-no-drag]')) return;

  const edge = edgeAt(event);
  if (edge) {
    gesture = 'resize';
    el.card.dataset.resizing = '1';
    send({ t: 'resizestart', edge: edge });
  } else {
    gesture = 'move';
    send({ t: 'dragstart' });
  }
});

function endGesture() {
  if (!gesture) return;
  send({ t: gesture === 'resize' ? 'resizeend' : 'dragend' });
  gesture = null;
  delete el.card.dataset.resizing;
  el.card.style.cursor = '';
}

// Listen on window: the pointer routinely leaves the card mid-gesture, and a
// pointerup missed there would leave the window stuck to the cursor.
window.addEventListener('pointerup', endGesture);
window.addEventListener('blur', endGesture);

document.getElementById('close')
  .addEventListener('click', () => send({ t: 'close' }));

/** User-set multiplier on top of the width-derived size (Settings > Text size). */
let textScale = 1.0;

/** Scale the lyric type with the card, so resizing changes how much is shown
 *  rather than just cropping a fixed-size layout. */
function scaleType() {
  const base = el.card.clientWidth * 0.047 * textScale;
  const size = Math.max(14, Math.min(64, base));
  document.documentElement.style.setProperty('--lyric-size', size.toFixed(1) + 'px');
}

window.addEventListener('resize', () => {
  scaleType();
  // Re-centre: a width change alters how lines wrap, and therefore their height.
  scrollToActive(state.activeIndex);
});

scaleType();

/* ----------------------------------------------------------------- start -- */

document.fonts.ready.then(() => {
  animate(
    el.card,
    { opacity: 1, y: 0, scale: 1 },
    { type: 'spring', bounce: 0.22, visualDuration: 0.55 }
  );
});

requestAnimationFrame(tick);
