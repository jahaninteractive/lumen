const JOB_ACTIVE = ['queued', 'running'];
const state = {
  groups: [], photoCount: 0, selectedId: null, selectedGroupId: null, status: null, profile: null, acting: false, composition: null, recommendedCropId: null, compositionRequest: 0, reviewRequest: 0,
  job: null, jobTimer: null, jobRequest: 0, details: null, showRejected: false, rejectedCount: 0, focusedId: null, compare: [],
  astro: { job: null, folders: { darks: null, flats: null, biases: null }, lightsKey: null },
  ingest: { card: null, primary: null, mirror: null },
  // Busy while a request is in flight or a background job is queued/running.
  get busy() { return this.acting || JOB_ACTIVE.includes(this.job?.status); },
  set busy(value) { this.acting = value; },
};
const sheet = document.querySelector('#contact-sheet');
const inspection = document.querySelector('#inspection');
const announcer = document.querySelector('#announcer');

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error || `Request failed (${response.status})`);
    error.payload = payload;
    throw error;
  }
  return payload;
}

function post(url, payload = {}) {
  return request(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
}

function announce(message) {
  announcer.textContent = '';
  window.setTimeout(() => { announcer.textContent = message; }, 10);
}

function cropLabel(kind) {
  return {
    original: 'Original',
    'subject-in-scene': 'Subject in scene',
    'tighter-subject': 'Tighter subject',
    'centered-fallback': 'Centered fallback',
  }[kind] || 'Crop option';
}

function cropCard(candidate) {
  const photoId = state.composition.photo_id;
  const card = document.createElement('article');
  card.className = 'crop-card';
  const activeId = state.composition.active_crop?.candidate_id;
  const active = candidate.kind === 'original' ? !activeId : candidate.id === activeId;
  if (active) card.classList?.add('active');
  if (candidate.id === state.recommendedCropId) card.classList?.add('recommended');

  const heading = document.createElement('h3');
  heading.textContent = cropLabel(candidate.kind);
  const viewport = document.createElement('div');
  viewport.className = 'crop-preview';
  viewport.style.aspectRatio = `${candidate.pixel_box[2] - candidate.pixel_box[0]} / ${candidate.pixel_box[3] - candidate.pixel_box[1]}`;
  const image = document.createElement('img');
  image.src = state.composition.preview_url;
  image.alt = `${heading.textContent} preview`;
  image.style.setProperty('--crop-left', candidate.rect.left);
  image.style.setProperty('--crop-top', candidate.rect.top);
  image.style.setProperty('--crop-width', candidate.rect.width);
  const thirds = document.createElement('span');
  thirds.className = 'rule-of-thirds';
  thirds.setAttribute('aria-hidden', 'true');
  viewport.append(image, thirds);

  const details = document.createElement('p');
  details.className = 'crop-details';
  const aspectLabel = candidate.aspect === 'native' ? 'Original ratio' : candidate.aspect;
  details.textContent = `${aspectLabel} · ${candidate.remaining_megapixels.toFixed(1)} MP remaining`;
  const rationale = document.createElement('p');
  rationale.className = 'quiet';
  rationale.textContent = candidate.rationale || 'Full frame, unchanged.';
  const warning = document.createElement('p');
  warning.className = 'crop-warning';
  warning.textContent = candidate.resolution_warning || '';
  const marker = document.createElement('p');
  marker.className = 'crop-marker';
  marker.textContent = `${active ? 'Current choice' : ''}${active && candidate.id === state.recommendedCropId ? ' · ' : ''}${candidate.id === state.recommendedCropId ? 'Local AI recommendation' : ''}`;

  const action = document.createElement('button');
  action.type = 'button';
  action.dataset.cropAction = candidate.id;
  action.className = candidate.kind === 'original' ? 'secondary' : '';
  action.textContent = candidate.kind === 'original' ? 'Keep original' : 'Use this crop';
  action.setAttribute('aria-label', candidate.kind === 'original'
    ? `Keep ${heading.textContent}, ${aspectLabel}`
    : `Use ${heading.textContent} crop, ${aspectLabel}`);
  action.disabled = state.busy || active;
  action.addEventListener('click', () => applyCrop(photoId, candidate));
  card.append(heading, viewport, details, rationale, warning, marker, action);
  return card;
}

function renderComposition() {
  const primary = document.querySelector('#primary-crops');
  const alternatives = document.querySelector('#alternative-crops');
  primary.replaceChildren();
  alternatives.replaceChildren();
  state.composition.candidates.forEach(candidate => {
    (candidate.revealable ? alternatives : primary).append(cropCard(candidate));
  });
  document.querySelector('#more-crop-shapes').hidden = alternatives.children.length === 0;
  document.querySelector('#compose-empty').hidden = true;
  document.querySelector('#compose-workspace').hidden = false;
  document.querySelector('#ask-composition-ai').disabled = state.busy;
}

async function loadComposition() {
  const requestId = ++state.compositionRequest;
  state.reviewRequest += 1;
  const photoId = state.selectedId;
  state.composition = null;
  state.recommendedCropId = null;
  document.querySelector('#composition-advice').textContent = '';
  document.querySelector('#compose-workspace').hidden = true;
  if (!photoId) {
    document.querySelector('#compose-empty').textContent = 'Select a photo from the contact sheet to see crop choices.';
    document.querySelector('#compose-empty').hidden = false;
    document.querySelector('#compose-workspace').hidden = true;
    document.querySelector('#compose-status').textContent = '';
    return;
  }
  document.querySelector('#compose-status').textContent = 'Loading crop choices…';
  try {
    const composition = await request(`/api/photos/${photoId}/crops`);
    if (requestId !== state.compositionRequest || state.selectedId !== photoId) return;
    state.composition = composition;
    state.recommendedCropId = null;
    renderComposition();
    document.querySelector('#compose-status').textContent = 'Choose a crop or keep the original. Your RAW file will not change.';
  } catch (error) {
    if (requestId !== state.compositionRequest || state.selectedId !== photoId) return;
    state.composition = null;
    document.querySelector('#compose-workspace').hidden = true;
    document.querySelector('#compose-empty').hidden = false;
    document.querySelector('#compose-empty').textContent = 'Crop choices are unavailable for this photo.';
    document.querySelector('#compose-status').textContent = error.message;
    announce(error.message);
  }
}

async function applyCrop(photoId, candidate) {
  if (state.selectedId !== photoId) return;
  state.compositionRequest += 1;
  state.reviewRequest += 1;
  state.recommendedCropId = null;
  document.querySelector('#composition-advice').textContent = '';
  state.busy = true;
  updateWorkflow();
  try {
    if (candidate.kind === 'original') await post(`/api/photos/${photoId}/crop/clear`);
    else await post(`/api/photos/${photoId}/crop`, { candidate_id: candidate.id });
    announce(candidate.kind === 'original' ? 'Original frame kept.' : `${cropLabel(candidate.kind)} selected.`);
    if (state.selectedId === photoId) await loadComposition();
  } catch (error) {
    document.querySelector('#compose-status').textContent = error.message;
    announce(error.message);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
}

async function askCompositionAI() {
  const photoId = state.selectedId;
  if (!photoId || !state.composition || state.composition.photo_id !== photoId) return;
  const requestId = ++state.reviewRequest;
  const compositionRequest = state.compositionRequest;
  state.busy = true;
  updateWorkflow();
  document.querySelector('#compose-status').textContent = 'Asking the loaded local vision model…';
  try {
    const review = await post(`/api/photos/${photoId}/composition-review`);
    if (requestId !== state.reviewRequest || compositionRequest !== state.compositionRequest || state.selectedId !== photoId) return;
    const candidate = state.composition.candidates.find(item => item.id === review.candidate_id);
    if (!candidate) {
      document.querySelector('#compose-status').textContent = 'Crop choices changed while the local AI was reviewing them. Reloading current choices…';
      announce('Crop choices changed. Reloading current choices.');
      await loadComposition();
      return;
    }
    state.recommendedCropId = review.candidate_id;
    renderComposition();
    const advice = `${cropLabel(candidate?.kind)} recommended (${Math.round(review.confidence * 100)}% confidence): ${review.rationale}`;
    document.querySelector('#composition-advice').textContent = advice;
    document.querySelector('#compose-status').textContent = 'Local AI advice is ready. It has not changed your crop.';
    announce(advice);
  } catch (error) {
    if (requestId !== state.reviewRequest || compositionRequest !== state.compositionRequest || state.selectedId !== photoId) return;
    const message = `Local AI advice is unavailable: ${error.message}. Manual crop choices still work.`;
    document.querySelector('#compose-status').textContent = message;
    announce(message);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
}

function metric(label, value) {
  const safe = Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;
  const wrapper = document.createElement('div');
  wrapper.className = 'metric';
  const caption = document.createElement('span');
  caption.innerHTML = `<b>${label}</b><em>${Math.round(safe * 100)}%</em>`;
  const meter = document.createElement('div');
  meter.className = 'meter';
  meter.setAttribute('role', 'meter');
  meter.setAttribute('aria-label', label);
  meter.setAttribute('aria-valuenow', String(Math.round(safe * 100)));
  const fill = document.createElement('i');
  fill.style.width = `${safe * 100}%`;
  meter.append(fill);
  wrapper.append(caption, meter);
  return wrapper;
}

const DECISION_WORDS = { flag: 'Flagged', reject: 'Rejected', none: 'No decision' };
const DECISION_ANNOUNCEMENTS = { flag: 'flagged', reject: 'rejected', none: 'decision cleared' };
const NO_KEEPER = 'No keeper in this burst: every frame is rejected.';

function allRejected(group) {
  return group.frame_count > 0 && group.rejected_count === group.frame_count;
}

function batchText(group, index) {
  return `Burst ${index + 1} · ${frameCount(group.frame_count)}${allRejected(group) ? ` · ${NO_KEEPER}` : ''}`;
}

// Same rule as Library.keeper_ids: not rejected, and flagged or the group's best frame.
function isKeeper(photo, group) {
  return photo.decision !== 'reject' && (photo.decision === 'flag' || photo.id === group.automatic_pick_id);
}

// The one decision path for the inspector and the keyboard: field is 'decision', 'rating', or 'pick'.
// focusAfter (inspector only) returns focus to the re-rendered control; keyboard focus stays on the frame.
async function decide(photo, field, value, focusAfter = null) {
  if (state.busy) return announce('Wait for the current change or job to finish.');
  const previous = photo.decision;
  state.busy = true;
  updateWorkflow();
  try {
    let result;
    if (field === 'pick') {  // Choosing a frame as best clears its rejection (Library.set_pick).
      await post(`/api/photos/${photo.id}/pick`);
      const group = state.groups.find(item => item.photos.includes(photo));
      result = { decision: previous === 'reject' ? 'none' : previous, rating: photo.rating, group_id: group?.id, automatic_pick_id: photo.id };
    } else result = await post(`/api/photos/${photo.id}/${field}`, { [field]: value });
    photo.decision = result.decision;
    photo.rating = result.rating;
    const delta = (result.decision === 'reject') - (previous === 'reject');
    state.rejectedCount += delta;
    const groupIndex = state.groups.findIndex(item => item.id === result.group_id);
    const group = state.groups[groupIndex];
    if (group) {
      group.automatic_pick_id = result.automatic_pick_id;
      group.rejected_count += delta;
      refreshFrames(group);
      const batch = document.querySelectorAll('.batch-button')[groupIndex];
      if (batch) batch.textContent = batchText(group, groupIndex);
    }
    const name = photo.name.replace(/\.[^.]+$/, '');
    announce(field === 'pick' ? `${photo.name} is now your best frame.`
      : field === 'rating' ? `Rating ${result.rating} set for ${name}` : `${name} ${DECISION_ANNOUNCEMENTS[result.decision]}`);
    if (state.selectedId === photo.id) {
      renderInspector(photo);
      if (focusAfter) document.querySelector(focusAfter)?.focus();
    }
  } catch (error) {
    announce(error.message);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
}

function decisionControls(photo) {
  const wrapper = document.createElement('div');
  const status = document.createElement('p');
  status.textContent = `Decision: ${DECISION_WORDS[photo.decision]} · Rating ${photo.rating}`;
  const buttons = document.createElement('div');
  buttons.className = 'decision-controls';
  buttons.setAttribute('role', 'group');
  buttons.setAttribute('aria-label', `Decision for ${photo.name}`);
  for (const [value, label] of [['flag', 'Flag'], ['reject', 'Reject'], ['none', 'Clear decision']]) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'secondary';
    button.dataset.decisionControl = value;
    button.textContent = label;
    button.addEventListener('click', () => decide(photo, 'decision', value, `[data-decision-control="${value}"]`));
    buttons.append(button);
  }
  const label = document.createElement('label');
  label.htmlFor = 'rating';
  label.textContent = 'Rating';
  const rating = document.createElement('select');
  rating.id = 'rating';
  for (let value = 0; value <= 5; value += 1) {
    const option = document.createElement('option');
    option.value = String(value);
    option.textContent = value ? `${value} star${value === 1 ? '' : 's'}` : 'No rating';
    rating.append(option);
  }
  rating.value = String(photo.rating);
  rating.addEventListener('change', () => decide(photo, 'rating', Number(rating.value), '#rating'));
  wrapper.append(status, buttons, label, rating);
  return wrapper;
}

async function inspect(photo) {
  if (state.selectedId !== photo.id) {
    state.compositionRequest += 1;
    state.reviewRequest += 1;
    state.recommendedCropId = null;
    document.querySelector('#composition-advice').textContent = '';
  }
  state.selectedId = photo.id;
  state.selectedGroupId = state.groups.find(group => group.photos.some(item => item.id === photo.id))?.id ?? null;
  updateWorkflow();
  const compositionLoad = document.querySelector('#compose-tab').getAttribute('aria-selected') === 'true' ? loadComposition() : null;
  document.querySelectorAll('.frame').forEach(button => button.setAttribute('aria-pressed', String(Number(button.dataset.id) === photo.id)));
  const details = await request(`/api/photos/${photo.id}`);
  if (state.selectedId !== photo.id) return;
  state.details = details;
  renderInspector(photo);
  if (compositionLoad) await compositionLoad;
}

function renderInspector(photo) {
  const details = state.details;
  inspection.replaceChildren();
  const heading = document.createElement('h3');
  heading.textContent = photo.name;
  const verdict = document.createElement('p');
  verdict.className = 'quiet';
  const group = state.groups.find(item => item.photos.includes(photo));
  verdict.textContent = group && allRejected(group) ? NO_KEEPER
    : photo.id === photo.groupPick ? 'Best measured frame in this group.' : 'Alternate frame; compare its component scores.';
  inspection.append(heading, verdict, decisionControls(photo));
  inspection.append(metric('Overall', photo.total_score), metric('Sharpness', details.metrics.sharpness), metric('Exposure', details.metrics.exposure));
  if (details.metrics.eye_quality !== null) inspection.append(metric('Eyes', details.metrics.eye_quality));
  if (details.tags?.data) {
    const description = document.createElement('p');
    description.textContent = details.tags.data.description;
    inspection.append(description);
  }
  const actions = document.createElement('div');
  actions.className = 'actions';
  if (photo.id !== photo.groupPick || photo.decision === 'reject') {  // Choosing a rejected frame clears its rejection.
    const choose = document.createElement('button');
    choose.type = 'button';
    choose.className = 'secondary';
    choose.textContent = 'Choose as best';
    choose.addEventListener('click', () => decide(photo, 'pick', null, `.frame[data-id="${photo.id}"]`));
    actions.append(choose);
  }
  const tag = document.createElement('button');
  tag.type = 'button';
  tag.textContent = 'Add local AI tags';
  tag.addEventListener('click', async () => {
    tag.disabled = true;
    try {
      await request(`/api/photos/${photo.id}/tag`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      announce(`Tags added to ${photo.name}`);
      await inspect(photo);
    } catch (error) {
      announce(error.message);
      tag.disabled = false;
    }
  });
  actions.append(tag);
  inspection.append(actions);
}

function fillFrame(button, photo, group) {
  photo.groupPick = group.automatic_pick_id;
  const best = photo.id === group.automatic_pick_id && photo.decision !== 'reject';
  const marks = [DECISION_WORDS[photo.decision], photo.rating ? `Rating ${photo.rating}` : null, state.compare.includes(photo.id) ? 'Compare' : null]
    .filter(word => word && word !== DECISION_WORDS.none);
  button.dataset.decision = photo.decision;
  button.setAttribute('aria-label', [`${photo.name}, score ${Math.round(photo.total_score * 100)} percent`, best ? 'best in group' : null, ...marks.map(word => word.toLowerCase())].filter(Boolean).join(', '));
  const image = document.createElement('img');
  image.src = photo.preview_url;
  image.alt = '';
  image.loading = 'lazy';
  const meta = document.createElement('span');
  meta.className = 'frame-meta';
  const name = document.createElement('span');
  name.textContent = photo.name;
  const score = document.createElement('span');
  score.className = 'score';
  score.textContent = String(Math.round(photo.total_score * 100));
  meta.append(name, score);
  if (marks.length) {
    const list = document.createElement('span');
    list.className = 'marks';
    marks.forEach(word => {
      const mark = document.createElement('span');
      mark.className = word.split(' ')[0].toLowerCase();
      mark.textContent = word;
      list.append(mark);
    });
    meta.append(list);
  }
  button.replaceChildren(image, meta);
  if (best) {
    const winner = document.createElement('span');
    winner.className = 'winner';
    winner.textContent = 'Best';
    button.append(winner);
  }
}

function frameCard(photoId) {
  return Array.from(document.querySelectorAll('.frame')).find(card => Number(card.dataset.id) === photoId);
}

// Refills one card in place (marks, Best badge); the node stays, so focus and listeners survive.
function refreshFrame(photoId) {
  const group = state.groups.find(item => item.photos.some(photo => photo.id === photoId));
  const card = frameCard(photoId);
  if (group && card) fillFrame(card, group.photos.find(photo => photo.id === photoId), group);
}

function refreshFrames(group) {
  group.photos.forEach(photo => refreshFrame(photo.id));
}

function renderFrame(photo, group) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'frame';
  button.dataset.id = photo.id;
  button.dataset.group = group.id;
  button.setAttribute('aria-pressed', String(photo.id === state.selectedId));
  button.setAttribute('tabindex', photo.id === state.focusedId ? '0' : '-1');
  fillFrame(button, photo, group);
  button.addEventListener('click', () => {
    focusFrame(photo.id);  // Safari does not focus a clicked button; culling keys need it focused
    inspect(photo).catch(error => announce(error.message));
  });
  button.addEventListener('focus', () => focusFrame(photo.id, false));  // Tab keeps the roving stop in step
  return button;
}

// Roving tabindex: only the focused frame is in the Tab order.
function focusFrame(photoId, move = true) {
  state.focusedId = photoId;
  document.querySelectorAll('.frame').forEach(card => card.setAttribute('tabindex', Number(card.dataset.id) === photoId ? '0' : '-1'));
  if (move) frameCard(photoId)?.focus();
}

function sheetColumns() {
  const columns = window.getComputedStyle?.(sheet).gridTemplateColumns;
  return columns && columns !== 'none' ? columns.trim().split(/\s+/).length : 1;
}

function toggleCompare(photo) {
  const marked = state.compare.includes(photo.id);
  const changed = marked ? [photo.id] : [photo.id, ...(state.compare.length >= 2 ? [state.compare.shift()] : [])];
  state.compare = marked ? state.compare.filter(id => id !== photo.id) : [...state.compare, photo.id];
  changed.forEach(refreshFrame);
  announce(marked ? 'Unmarked' : `Marked for compare (${state.compare.length} of 2)`);
}

const CULLING_KEYS = { p: 'pick', x: 'reject', f: 'flag', u: 'clear', c: 'compare', Enter: 'loupe', Escape: 'close' };
const CULLING_ACTIONS = {
  pick: photo => decide(photo, 'pick'),
  reject: photo => decide(photo, 'decision', 'reject'),
  flag: photo => decide(photo, 'decision', 'flag'),
  clear: photo => decide(photo, 'decision', 'none'),
  compare: toggleCompare,
  loupe: null,  // Task 5 adds the loupe; until then Enter keeps its native click (inspect).
  close: null,  // Task 5 closes compare/loupe views.
};

function handleCullingKey(event) {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  const target = event.target;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target?.tagName) || (target?.tagName === 'BUTTON' && target.closest?.('dialog form'))) return;
  const key = event.key?.length === 1 ? event.key.toLowerCase() : event.key;
  const ids = Array.from(document.querySelectorAll('.frame'), card => Number(card.dataset.id));
  const index = ids.indexOf(state.focusedId);
  const row = sheetColumns();
  const next = { ArrowLeft: index - 1, ArrowRight: index + 1, ArrowUp: index - row, ArrowDown: index + row, PageUp: index - row, PageDown: index + row, Home: 0, End: ids.length - 1 }[key];
  if (next !== undefined) {
    if (!ids.length) return;
    event.preventDefault();
    return focusFrame(ids[Math.max(0, Math.min(ids.length - 1, next))]);
  }
  const action = /^[0-5]$/.test(key) ? photo => decide(photo, 'rating', Number(key)) : CULLING_ACTIONS[CULLING_KEYS[key]];
  if (!action) return;
  event.preventDefault();
  const photo = state.groups.flatMap(group => group.photos).find(item => item.id === state.focusedId);
  return photo ? action(photo) : undefined;
}

function renderGroups() {
  sheet.replaceChildren();
  const rail = document.querySelector('#batch-list');
  rail.replaceChildren();
  let count = 0;
  const ids = state.groups.flatMap(group => group.photos.map(photo => photo.id));
  if (!ids.includes(state.focusedId)) state.focusedId = ids.includes(state.selectedId) ? state.selectedId : ids[0] ?? null;
  state.compare = state.compare.filter(id => ids.includes(id));
  state.groups.forEach((group, groupIndex) => {
    const batch = document.createElement('button');
    batch.type = 'button';
    batch.className = 'batch-button';
    batch.textContent = batchText(group, groupIndex);
    batch.setAttribute('aria-pressed', String(group.id === state.selectedGroupId));
    batch.addEventListener('click', () => {
      document.querySelector(`[data-group="${group.id}"]`)?.focus();
      if (group.photos[0]) inspect(group.photos[0]).catch(error => announce(error.message));
      else {  // An all-rejected burst with its frames hidden can still be selected, e.g. to remove it.
        state.selectedId = null;
        state.selectedGroupId = group.id;
        inspection.textContent = NO_KEEPER;
        updateWorkflow();
        announce(batch.textContent);
      }
    });
    rail.append(batch);
    group.photos.forEach(photo => {
      count += 1;
      sheet.append(renderFrame(photo, group));
    });
  });
  if (count === 0) {
    const empty = document.createElement('p');
    empty.className = 'quiet';
    empty.textContent = 'Import a folder to build your first contact sheet.';
    sheet.append(empty);
  }
  document.querySelector('#photo-count').textContent = `${count} frame${count === 1 ? '' : 's'}`;
  if (!count) rail.textContent = 'No imported batches yet.';
  updateWorkflow();
}

async function load() {
  const [status, groups] = await Promise.all([request('/api/status'), request(state.showRejected ? '/api/groups?show_rejected=1' : '/api/groups')]);
  state.status = status;
  state.groups = groups.groups;
  state.rejectedCount = groups.rejected_count || 0;
  state.photoCount = groups.photo_count;
  const selectedGroup = state.groups.find(group => group.photos.some(photo => photo.id === state.selectedId))
    || (state.selectedId === null ? state.groups.find(group => group.id === state.selectedGroupId) : undefined);
  state.selectedGroupId = selectedGroup?.id ?? null;
  if (!selectedGroup) {
    state.selectedId = null;
    inspection.textContent = 'Select a frame to see why it ranked where it did.';
  }
  document.querySelector('#system-status').textContent = status.omlx_live.summary;
  document.querySelector('#export-destination').textContent = status.jpeg_output_dir;
  renderGroups();
  if (!state.astro.job) {
    let remembered = null;
    try { remembered = localStorage.getItem('lumen-astro-job'); } catch { /* per-visit only */ }
    if (remembered) await request(`/api/astro/jobs/${remembered}`).then(job => (job.status === 'running' ? watchJob(job) : setAstroJob(job))).catch(() => localStorage.removeItem('lumen-astro-job'));
  }
  if (!JOB_ACTIVE.includes(state.job?.status)) {
    // Pick up a job started from the CLI or another tab; a prepared astro stack stays queued until Run, so it is not active work.
    const active = (await request('/api/jobs')).jobs.find(job => JOB_ACTIVE.includes(job.status) && !(job.kind === 'astro' && job.status === 'queued'));
    if (active) watchJob(active);
  }
  if (document.querySelector('#compose-tab').getAttribute('aria-selected') === 'true') await loadComposition();
}

document.querySelector('#import-form').addEventListener('submit', async event => {
  event.preventDefault();
  state.busy = true;
  updateWorkflow();
  document.querySelector('#import-status').textContent = 'Starting import…';
  try {
    watchJob(await post('/api/import', { path: document.querySelector('#import-path').value }));
    document.querySelector('#import-status').textContent = 'Import started. Progress and results appear in the job strip at the top.';
  } catch (error) {
    document.querySelector('#import-status').textContent = error.message;
    announce(error.message);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
});

const JOB_NAMES = { import: 'Import', tag: 'Tagging', astro: 'Stack', ingest: 'Ingest' };

function jobText(job) {
  const name = JOB_NAMES[job.kind] || 'Job';
  const result = job.result || {};
  const summary = job.kind === 'import' && job.result ? ` ${result.imported} new, ${result.updated} updated, ${result.skipped} skipped, ${result.failed} failed.`
    : job.kind === 'tag' && job.result ? ` ${result.tagged} tagged, ${result.skipped} skipped, ${result.failed} failed.` : '';
  const verdict = job.kind === 'ingest' ? ` ${ingestVerdict(job)}` : '';
  return {
    queued: `${name} queued.`,
    running: `${name} running.`,
    succeeded: `${name} finished.${summary}${verdict}`,
    failed: `${name} failed: ${job.error || 'unknown error'}.${verdict}`,
    cancelled: `${name} cancelled. Finished items are kept.${verdict}`,
    interrupted: `${name} interrupted by a Lumen restart. Start it again to resume.${verdict}`,
  }[job.status] || `${name} status: ${job.status}.`;
}

function watchJob(job) {
  const previous = state.job?.job_id === job.job_id ? state.job.status : null;
  state.job = job;
  state.jobRequest += 1;  // any newer state invalidates polls already in flight
  const percent = Math.round((job.progress || 0) * 100);
  const progress = document.querySelector('#job-progress');
  progress.value = percent;
  progress.setAttribute('aria-valuetext', `${percent}% complete`);
  document.querySelector('#job-label').textContent = `${JOB_NAMES[job.kind] || 'Job'} ${job.job_id}${job.total ? ` · ${job.processed} of ${job.total}` : ''}${job.label ? ` · ${job.label}` : ''}`;
  const status = document.querySelector('#job-status');
  const text = jobText(job);
  if (status.textContent !== text) status.textContent = text;  // polite live region: no identical rewrites each poll
  document.querySelector('#job-strip').hidden = false;
  if (job.kind === 'astro') setAstroJob(job);
  else {
    if (job.kind === 'ingest') renderIngest(job);
    if (job.status !== previous) announce(text);
  }
  if (JOB_ACTIVE.includes(job.status)) startJobPolling();
  else {
    stopJobPolling();
    if (JOB_ACTIVE.includes(previous)) load().catch(error => announce(error.message));
  }
  updateWorkflow();
}

function startJobPolling() {
  if (state.jobTimer !== null) return;
  state.jobTimer = window.setInterval(pollJob, 1000);
}

function stopJobPolling() {
  if (state.jobTimer === null) return;
  window.clearInterval(state.jobTimer);
  state.jobTimer = null;
}

async function pollJob() {
  const job = state.job;
  if (!job) return stopJobPolling();
  const requestId = state.jobRequest;
  try {
    const latest = await request(`/api/jobs/${job.job_id}`);
    if (requestId === state.jobRequest) watchJob(latest);
  } catch (error) {
    stopJobPolling();
    document.querySelector('#job-status').textContent = `${error.message}. Reload this page to resume watching the job.`;
    announce(document.querySelector('#job-status').textContent);
  }
}

document.querySelector('#cancel-job').addEventListener('click', async () => {
  state.busy = true;
  updateWorkflow();
  try {
    watchJob(await post(`/api/jobs/${state.job.job_id}/cancel`));
  } catch (error) {
    document.querySelector('#job-status').textContent = `Cancel failed: ${error.message}`;
    announce(document.querySelector('#job-status').textContent);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
});

document.querySelector('#browse-path').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await request('/api/browse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    if (!result.cancelled && result.path) {
      const input = document.querySelector('#import-path');
      input.value = result.path;
      input.focus();
      announce(`Selected folder: ${result.path}`);
    }
  } catch (error) {
    announce(error.message);
  } finally {
    button.disabled = false;
  }
});

// The only two verdicts Lumen gives about a card; nothing else may suggest it is safe to reuse.
function ingestVerdict(job) {
  const counts = job.ingest || {};
  if (job.status === 'succeeded' && counts.card_verified_everywhere === true) return 'Every file verified on every destination.';
  const missing = Math.max((counts.files || 0) - (counts.verified || 0), 0) + (counts.unreadable || 0);
  return `Do not format the card: ${missing} ${missing === 1 ? 'file was' : 'files were'} not verified.`;
}

function renderIngest(job) {
  const counts = job.ingest || {};
  const skipped = (counts.unreadable ? ` · ${counts.unreadable} ${counts.unreadable === 1 ? 'entry' : 'entries'} on the card could not be read` : '')
    + (counts.skipped ? ` · ${counts.skipped} hidden ${counts.skipped === 1 ? 'entry' : 'entries'} skipped` : '');
  document.querySelector('#ingest-counts').textContent = `${counts.copied || 0} copied · ${counts.already_present || 0} already present · ${counts.failed || 0} failed · ${counts.verified || 0} of ${counts.files || 0} files verified on every destination${skipped}`;
  document.querySelector('#ingest-manifest').textContent = `${state.status?.ingest_output_dir || '<library>/outputs/ingest'}/${job.job_id}/${counts.manifest || 'manifest.json'}`;
  document.querySelector('#ingest-snapshot').textContent = counts.snapshot ? `Catalog snapshot: lumen-catalog-backups/${counts.snapshot} in the primary destination.` : '';
  document.querySelector('#ingest-verdict').textContent = JOB_ACTIVE.includes(job.status) ? '' : ingestVerdict(job);
}

async function ingestAction(label, action) {
  state.busy = true;
  updateWorkflow();
  try {
    await action();
  } catch (error) {
    document.querySelector('#ingest-status').textContent = `${label} failed: ${error.message}`;
    announce(document.querySelector('#ingest-status').textContent);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
}

for (const kind of ['card', 'primary', 'mirror']) {
  document.querySelector(`#browse-ingest-${kind}`).addEventListener('click', () => ingestAction(`Choose ${kind} folder`, async () => {
    const result = await post('/api/browse');
    if (!result.cancelled && result.path) {
      state.ingest[kind] = result.path;
      document.querySelector(`#ingest-${kind}-path`).textContent = result.path;
      announce(`${kind} folder selected.`);
    }
  }));
}
document.querySelector('#reset-ingest-mirror').addEventListener('click', () => {
  state.ingest.mirror = null;
  document.querySelector('#ingest-mirror-path').textContent = 'No mirror';
  updateWorkflow();
});
document.querySelector('#start-ingest').addEventListener('click', () => ingestAction('Start ingest', async () => {
  document.querySelector('#ingest-status').textContent = 'Starting ingest…';
  watchJob(await post('/api/ingest', { card: state.ingest.card, primary: state.ingest.primary, mirror: state.ingest.mirror }));
  document.querySelector('#ingest-status').textContent = 'Ingest started. The card is only read; progress appears in the job strip.';
}));

const ASTRO_WORDS = { queued: 'Stack prepared and queued.', running: 'Stack running.', succeeded: 'Stack finished.', failed: 'Stack failed.', cancelled: 'Stack cancelled.', interrupted: 'Stack interrupted.' };

function renderAstroLights(group) {
  const key = group ? group.photos.map(photo => photo.id).join(',') : null;
  if (key === state.astro.lightsKey) return;
  state.astro.lightsKey = key;
  const list = document.querySelector('#astro-lights');
  list.replaceChildren();
  (group?.photos || []).forEach(photo => {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = true;
    input.dataset.id = photo.id;
    input.addEventListener('change', updateWorkflow);
    const name = document.createElement('span');
    name.textContent = photo.name;
    label.append(input, name);
    list.append(label);
  });
}

function astroLights() {
  const inputs = Array.from(document.querySelectorAll('#astro-lights input'));
  return { included: inputs.filter(input => input.checked).length, excluded: inputs.filter(input => !input.checked).map(input => Number(input.dataset.id)) };
}

function astroPath(name) {
  return `${state.status?.astro_output_dir || '<library>/outputs/astro'}/${state.astro.job.job_id}/${name}`;
}

function setAstroJob(job) {
  const previous = state.astro.job?.job_id === job.job_id ? state.astro.job.status : null;
  state.astro.job = job;
  const status = document.querySelector('#astro-status');
  const progress = document.querySelector('#astro-progress');
  const percent = Math.round((job.progress || 0) * 100);
  progress.value = percent;
  progress.setAttribute('aria-valuetext', `${percent}% complete`);
  document.querySelector('#astro-summary').textContent = `Job ${job.job_id} · ${job.lights} lights, ${job.excluded_lights} excluded · ${job.darks} darks · ${job.flats} flats · ${job.biases} biases`;
  const warnings = document.querySelector('#astro-warnings');
  warnings.replaceChildren();
  job.warnings.forEach(text => {
    const item = document.createElement('li');
    item.textContent = `Warning: ${text}`;
    warnings.append(item);
  });
  document.querySelector('#astro-log').textContent = job.log_tail.join('\n');
  const paths = document.querySelector('#astro-paths');
  paths.replaceChildren();
  const files = [['Manifest', 'manifest.json'], ['Siril log', 'siril.log']];
  if (job.result) files.push(['Result (16-bit TIFF)', job.result.name]);
  for (const [label, name] of files) {
    const caption = document.createElement('p');
    caption.textContent = label;
    const value = document.createElement('p');
    value.className = 'path';
    value.textContent = astroPath(name);
    paths.append(caption, value);
  }
  const text = {
    queued: 'Stack prepared and queued. Review the warnings, then choose Run stack. Cancel this stack to change the selection.',
    running: `Stacking… about ${Math.floor(percent / 10) * 10}% complete. Cancel stack stops only this job's Siril process.`,
    succeeded: 'Stack finished. The 16-bit result TIFF and provenance are recorded; open the job folder to develop it further.',
    failed: `Stack failed: ${job.error || 'unknown error'}. The manifest and Siril log remain in the job folder for review.`,
    cancelled: 'Stack cancelled. No result was recorded; the manifest and log remain in the job folder.',
    interrupted: 'Stack was interrupted by a Lumen restart. Prepare a new stack; the manifest and log remain for review.',
  }[job.status] || `Stack status: ${job.status}.`;
  if (status.textContent !== text) status.textContent = text;  // polite live region: no identical rewrites each poll
  if (job.status !== previous) announce(ASTRO_WORDS[job.status] || `Stack ${job.status}.`);
  try {
    if (JOB_ACTIVE.includes(job.status)) localStorage.setItem('lumen-astro-job', String(job.job_id));
    else localStorage.removeItem('lumen-astro-job');
  } catch { /* per-visit only */ }
  updateWorkflow();
}

async function astroAction(label, action) {
  state.busy = true;
  updateWorkflow();
  try {
    await action();
  } catch (error) {
    document.querySelector('#astro-status').textContent = `${label} failed: ${error.message}`;
    announce(document.querySelector('#astro-status').textContent);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
}

document.querySelector('#prepare-astro').addEventListener('click', () => astroAction('Prepare stack', async () => {
  const { excluded } = astroLights();
  setAstroJob(await post('/api/astro/jobs', { group_id: state.selectedGroupId, confirmed: true, excluded_photo_ids: excluded, ...state.astro.folders }));
}));
document.querySelector('#run-astro').addEventListener('click', () => astroAction('Run stack', async () => {
  watchJob(await post(`/api/astro/jobs/${state.astro.job.job_id}/run`));  // queued until the worker flips the row; polling starts now
}));
document.querySelector('#cancel-astro').addEventListener('click', () => astroAction('Cancel stack', async () => {
  watchJob(await post(`/api/astro/jobs/${state.astro.job.job_id}/cancel`));
}));
document.querySelector('#reveal-astro').addEventListener('click', () => astroAction('Finder reveal', async () => {
  const result = await post(`/api/astro/jobs/${state.astro.job.job_id}/reveal`);
  document.querySelector('#astro-status').textContent = `Opened job folder in Finder: ${result.path}`;
}));
document.querySelector('#astro-confirm').addEventListener('change', updateWorkflow);
for (const kind of ['darks', 'flats', 'biases']) {
  const show = () => { document.querySelector(`#astro-${kind}-path`).textContent = state.astro.folders[kind] || 'None'; updateWorkflow(); };
  document.querySelector(`#browse-astro-${kind}`).addEventListener('click', () => astroAction(`Choose ${kind} folder`, async () => {
    const result = await post('/api/browse');
    if (!result.cancelled && result.path) { state.astro.folders[kind] = result.path; announce(`${kind} folder selected.`); }
    show();
  }));
  document.querySelector(`#reset-astro-${kind}`).addEventListener('click', () => { state.astro.folders[kind] = null; show(); });
}

function frameCount(count) {
  return `${count} frame${count === 1 ? '' : 's'}`;
}

function updateWorkflow() {
  const photos = state.groups.flatMap(group => group.photos);
  const photo = photos.find(item => item.id === state.selectedId);
  const groupIndex = state.groups.findIndex(item => item.id === state.selectedGroupId);
  const group = state.groups[groupIndex];
  const keepers = state.groups.reduce((total, item) => total + item.photos.filter(photo => isKeeper(photo, item)).length, 0);
  const available = state.status?.tools?.['darktable-cli']?.available === true;
  document.querySelector('#current-export-name').textContent = photo?.name || 'No photo selected';
  document.querySelector('#keeper-count').textContent = `${keepers} keeper${keepers === 1 ? '' : 's'}`;
  document.querySelector('#rejected-count').textContent = `(${state.rejectedCount})`;
  document.querySelectorAll('[data-decision-control]').forEach(button => { button.disabled = state.busy; });
  const rating = document.querySelector('#rating');
  if (rating) rating.disabled = state.busy;
  document.querySelector('#group-cleanup-count').textContent = group ? `Burst ${groupIndex + 1} · ${frameCount(group.frame_count)}` : 'No batch selected · 0 frames';
  document.querySelector('#workspace-cleanup-count').textContent = `${state.groups.length} batch${state.groups.length === 1 ? '' : 'es'} · ${frameCount(state.photoCount)}`;
  document.querySelector('#clear-help').textContent = `Use the exact uppercase word CLEAR. This removes all ${frameCount(state.photoCount)} from Lumen, including photos outside visible batches.`;
  document.querySelector('#export-availability').textContent = available ? 'darktable is ready. Existing JPEGs are kept; each export gets a unique name.' : 'darktable-cli is unavailable. Install darktable or add it to PATH, restart Lumen, then reload this page.';
  const scope = document.querySelector('input[name="export-scope"]:checked').value;
  document.querySelector('#export-jpegs').disabled = state.busy || !available || (scope === 'current' ? !photo : !keepers);
  document.querySelector('#remove-group').disabled = state.busy || !group;
  document.querySelector('#clear-workspace').disabled = state.busy || !state.photoCount || document.querySelector('#clear-confirmation').value !== 'CLEAR';
  document.querySelector('#browse-xmp').disabled = state.busy;
  document.querySelector('#reset-xmp').disabled = state.busy || !state.profile;
  document.querySelector('#import-form button[type="submit"]').disabled = state.busy;
  document.querySelector('#browse-path').disabled = state.busy;
  document.querySelector('#ask-composition-ai').disabled = state.busy || !state.composition || state.composition.photo_id !== state.selectedId;
  document.querySelectorAll('[data-crop-action]').forEach(button => { button.disabled = state.busy || button.closest?.('.crop-card')?.classList?.contains('active'); });
  document.querySelectorAll('.batch-button').forEach((button, index) => button.setAttribute('aria-pressed', String(state.groups[index].id === state.selectedGroupId)));

  renderAstroLights(group);
  const siril = state.status?.tools?.['siril-cli']?.available === true;
  const job = state.astro.job;
  const activeJob = JOB_ACTIVE.includes(job?.status);
  document.querySelector('#astro-group-count').textContent = group ? `Burst ${groupIndex + 1} · ${frameCount(group.photos.length)}` : 'No batch selected';
  document.querySelector('#astro-availability').textContent = siril ? 'Siril is ready. One stack runs at a time; everything is written below the library outputs folder.' : 'siril-cli is unavailable. Install Siril or add it to PATH, restart Lumen, then reload this page.';
  document.querySelector('#prepare-astro').disabled = state.busy || !siril || !group || !document.querySelector('#astro-confirm').checked || astroLights().included < 2 || activeJob;
  document.querySelector('#run-astro').disabled = state.busy || job?.status !== 'queued';
  document.querySelectorAll('#astro-lights input').forEach(input => { input.disabled = activeJob; });
  document.querySelector('#astro-confirm').disabled = activeJob;
  document.querySelector('#cancel-astro').disabled = state.acting || !activeJob;
  document.querySelector('#cancel-job').disabled = state.acting || !JOB_ACTIVE.includes(state.job?.status);
  document.querySelector('#reveal-astro').disabled = state.busy || !job;
  for (const kind of ['card', 'primary', 'mirror']) document.querySelector(`#browse-ingest-${kind}`).disabled = state.busy;
  document.querySelector('#reset-ingest-mirror').disabled = state.busy || !state.ingest.mirror;
  document.querySelector('#start-ingest').disabled = state.busy || !state.ingest.card || !state.ingest.primary;
  for (const kind of ['darks', 'flats', 'biases']) {
    document.querySelector(`#browse-astro-${kind}`).disabled = state.busy || activeJob;
    document.querySelector(`#reset-astro-${kind}`).disabled = state.busy || activeJob || !state.astro.folders[kind];
  }
}

const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
function selectTab(tab) {
  tabs.forEach(item => {
    const selected = item === tab;
    item.setAttribute('aria-selected', String(selected));
    item.tabIndex = selected ? 0 : -1;
    document.querySelector(`#${item.getAttribute('aria-controls')}`).hidden = !selected;
  });
  if (tab.id === 'compose-tab') loadComposition();
}
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => selectTab(tab));
  tab.addEventListener('keydown', event => {
    const next = { ArrowRight: (index + 1) % tabs.length, ArrowLeft: (index + tabs.length - 1) % tabs.length, Home: 0, End: tabs.length - 1 }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    selectTab(tabs[next]);
    tabs[next].focus();
  });
});

function rememberProfile(path) {
  state.profile = path;
  document.querySelector('#profile-path').textContent = path || 'Camera-aware defaults';
  try {
    if (path) localStorage.setItem('lumen-xmp-profile', path);
    else localStorage.removeItem('lumen-xmp-profile');
  } catch {
    document.querySelector('#export-status').textContent = 'Browser storage is unavailable; choose the XMP again next visit.';
  }
  updateWorkflow();
}

document.querySelector('#browse-xmp').addEventListener('click', async () => {
  state.busy = true;
  updateWorkflow();
  const status = document.querySelector('#export-status');
  status.textContent = 'Choose an existing XMP file in the macOS picker…';
  try {
    const result = await post('/api/browse-xmp');
    status.textContent = result.cancelled ? 'XMP selection cancelled; the current profile is unchanged.' : 'XMP selected for the next export.';
    if (!result.cancelled) rememberProfile(result.path);
  } catch (error) {
    status.textContent = `${error.message}. Choose an XMP again or use camera defaults.`;
    announce(status.textContent);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
});
document.querySelector('#reset-xmp').addEventListener('click', () => rememberProfile(null));
document.querySelectorAll('input[name="export-scope"]').forEach(input => input.addEventListener('change', updateWorkflow));
document.querySelector('#clear-confirmation').addEventListener('input', updateWorkflow);
document.querySelector('#show-rejected').addEventListener('change', () => {
  state.showRejected = document.querySelector('#show-rejected').checked;
  return load().catch(error => announce(error.message));
});
document.querySelector('#ask-composition-ai').addEventListener('click', askCompositionAI);
sheet.addEventListener('keydown', handleCullingKey);
const shortcuts = document.querySelector('#shortcuts');
document.querySelector('#shortcuts-button').addEventListener('click', () => {
  if (shortcuts.showModal) shortcuts.showModal();
  else shortcuts.hidden = false;
});
shortcuts.addEventListener('keydown', event => {  // a modal <dialog> closes on Escape natively
  if (event.key === 'Escape' && !shortcuts.showModal) shortcuts.hidden = true;
});

function showExportResults(results) {
  const list = document.querySelector('#export-results');
  list.replaceChildren();
  results.forEach(result => {
    const item = document.createElement('li');
    item.textContent = result.success ? result.output : `Photo ${result.photo_id}: ${result.message}`;
    if (result.success) item.className = 'path';
    list.append(item);
  });
  const succeeded = results.filter(result => result.success).length;
  document.querySelector('#export-status').textContent = `Export complete: ${succeeded} saved, ${results.length - succeeded} failed.${succeeded < results.length ? ' Successful JPEGs remain; fix the reported problem and retry the failed photo.' : ''}`;
}

document.querySelector('#export-jpegs').addEventListener('click', async () => {
  const scope = document.querySelector('input[name="export-scope"]:checked').value;
  const payload = { scope, profile: state.profile };
  if (scope === 'current') payload.photo_id = state.selectedId;
  state.busy = true;
  updateWorkflow();
  document.querySelector('#export-status').textContent = `Exporting ${scope === 'current' ? 'current photo' : 'all keepers'}… Keep this page open until the result appears.`;
  document.querySelector('#export-results').replaceChildren();
  try {
    showExportResults((await post('/api/export', payload)).results);
  } catch (error) {
    if (error.payload?.results) showExportResults(error.payload.results);
    else document.querySelector('#export-status').textContent = `${error.message}. Check darktable and the selected XMP, then retry. You can also use camera defaults.`;
    announce(document.querySelector('#export-status').textContent);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
});

document.querySelector('#reveal-outputs').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await post('/api/outputs/reveal');
    document.querySelector('#export-status').textContent = `Opened outputs in Finder: ${result.path}`;
  } catch (error) {
    document.querySelector('#export-status').textContent = `${error.message}. Open the destination shown above in Finder.`;
    announce(error.message);
  } finally {
    button.disabled = false;
  }
});

async function cleanup(url, payload) {
  state.busy = true;
  updateWorkflow();
  const status = document.querySelector('#clear-status');
  status.textContent = 'Moving Lumen working files into recoverable trash…';
  let removed = false;
  try {
    const result = await post(url, payload);
    removed = true;
    status.textContent = `Removed ${frameCount(result.photos_removed)} and moved ${result.files_removed} working files. Camera originals and completed exports remain.`;
    const recovery = document.querySelector('#recovery-paths');
    recovery.replaceChildren();
    for (const [label, path] of [['Recovery folder', result.trash_path], ['Catalog snapshot', result.snapshot_path]]) {
      const caption = document.createElement('p');
      caption.textContent = label;
      const value = document.createElement('p');
      value.className = 'path';
      value.textContent = path;
      recovery.append(caption, value);
    }
    document.querySelector('#clear-confirmation').value = '';
    state.selectedId = null;
    state.selectedGroupId = null;
    state.groups = url === '/api/library/clear' ? [] : state.groups.filter(group => `/api/groups/${group.id}/remove` !== url);
    state.photoCount = url === '/api/library/clear' ? 0 : Math.max(0, state.photoCount - result.photos_removed);
    inspection.textContent = 'Select a frame to see why it ranked where it did.';
    renderGroups();
    await load();
    document.querySelector('#clear-tab').focus();
  } catch (error) {
    status.textContent = removed ? `${status.textContent} Could not refresh batches: ${error.message}. Reload this page.` : `${error.message}. No cleanup was completed; retry after resolving the problem.`;
    announce(status.textContent);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
}
document.querySelector('#remove-group').addEventListener('click', () => cleanup(`/api/groups/${state.selectedGroupId}/remove`, {}));
document.querySelector('#clear-workspace').addEventListener('click', () => cleanup('/api/library/clear', { confirmation: document.querySelector('#clear-confirmation').value }));

try {
  const remembered = localStorage.getItem('lumen-xmp-profile');
  if (remembered) {
    state.profile = remembered;
    document.querySelector('#profile-path').textContent = remembered;
  }
} catch {
  document.querySelector('#export-status').textContent = 'Browser storage is unavailable; choose an XMP for this visit.';
}
load().catch(error => announce(error.message));
