const JOB_ACTIVE = ['queued', 'running'];
const CULLING_JOBS = ['import', 'ingest'];  // jobs that only add frames, so culling may continue alongside them
const state = {
  groups: [], photoCount: 0, selectedId: null, selectedGroupId: null, status: null, profile: null, acting: false, composition: null, critique: null, recommendedCropId: null, compositionRequest: 0, reviewRequest: 0,
  rejectSuggestions: [], job: null, jobTimer: null, jobRequest: 0, details: null, rejectedCount: 0, keeperCount: 0, shown: 200, focusedId: null, compare: [],
  astro: { job: null, folders: { darks: null, flats: null, biases: null }, lightsKey: null, triage: null },
  // Develop holds only the fields the photographer touched; saved is the last body the server was given.
  develop: { photoId: null, fields: {}, saved: '{}', ready: false, neutral: null, defaults: {}, lensCorrection: true, zoomShown: false, histogram: {}, controller: null, drag: null, beforeAdvice: null, advice: null },
  mergeSelection: new Set(), clearedFrames: 0,
  aiTasks: new Map(),  // per-panel local AI status by key, e.g. critique:<groupId>; it outlives the panel that asked
  ingest: { card: null, primary: null, mirror: null },
  // Busy while a request is in flight or a background job is queued/running.
  get busy() { return this.acting || JOB_ACTIVE.includes(this.job?.status); },
  set busy(value) { this.acting = value; },
  // Decisions, ratings, picks, compare, and loupe wait only for a request in flight, or for a job that rewrites what frames show.
  get cullingBlocked() { return this.acting || (JOB_ACTIVE.includes(this.job?.status) && !CULLING_JOBS.includes(this.job.kind)); },
};
const sheet = document.querySelector('#contact-sheet');
const inspection = document.querySelector('#inspection');
const announcer = document.querySelector('#announcer');

async function request(url, options = {}) {
  let response;
  try {
    response = await fetch(url, options);
  } catch {  // fetch itself failed: the server went away, as when lumen serve restarts mid-request
    throw new Error('Lumen server unreachable. If you just restarted lumen serve, reload this page.');
  }
  // A route this server does not have answers with the stdlib's HTML error page, not JSON.
  // Reading the body before the status reported that page as "Unexpected token '<'", which
  // named neither the status nor the likely cause.
  let payload = {};
  let unreadable = false;
  try {
    payload = await response.json();
  } catch {
    unreadable = true;
  }
  if (!response.ok) {
    const stale = unreadable ? ' Lumen may be older than this page: restart lumen serve and reload.' : '';
    const error = new Error((payload.error || `Request failed (${response.status}).`) + stale);
    error.payload = payload;
    throw error;
  }
  if (unreadable) throw new Error('Lumen sent a reply this page could not read.');
  return payload;
}

function post(url, payload = {}) {
  return request(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
}

// Every AI control waits for its role's model to be loaded in oMLX right now; the reason is spelled out, never only greyed.
function roleReady(role) {
  return state.status?.omlx_roles?.[role]?.loaded === true;
}

function roleUnavailable(role) {
  const item = state.status?.omlx_roles?.[role];
  return item && !item.loaded ? `${role === 'fast' ? 'Fast' : 'Review'} model ${item.model} is not loaded in oMLX. Load it in oMLX, then reload this page.` : '';
}

// Bottom status bar: every local AI request reports running, finished, or failed with the reason, in one place.
const aiStatusLine = document.querySelector('#ai-status');
function aiStatus(phase, text) {
  aiStatusLine.dataset.phase = phase;
  aiStatusLine.textContent = text;
}
// Advice never sets busy: the panel that asked shows its own line, updated in place, and finds it again on return.
const AI_TASK_LABELS = { critique: 'Burst pick', composition: 'Crop suggestion', develop: 'Develop suggestion', caption: 'Caption', tag: 'Tags' };
const aiTaskNodes = new Map();
function paintAiTask(node, key) {
  const task = state.aiTasks.get(key);
  node.hidden = !task;
  node.dataset.phase = task?.phase || '';
  node.textContent = task?.text || '';
}
// Always returns the line, hidden while there is no task, so a panel can place it once and forget it.
function aiTaskLine(key, node = document.createElement('p')) {
  node.className = 'ai-task';
  node.dataset.key = key;
  aiTaskNodes.set(key, node);
  paintAiTask(node, key);
  return node;
}
function refreshAiTask(key) {
  const node = aiTaskNodes.get(key);
  if (node?.dataset.key === key) paintAiTask(node, key);  // a panel's fixed line may since show another frame's task
}
function aiRunning(key) {
  return state.aiTasks.get(key)?.phase === 'running';
}
function aiTask(key, phase, text) {
  if (!key) return;
  state.aiTasks.set(key, { phase, text: `${AI_TASK_LABELS[key.split(':')[0]]} ${text}` });
  refreshAiTask(key);
}
async function aiCall(label, action, key = null) {
  aiStatus('running', `${label} running…`);
  aiTask(key, 'running', 'running…');
  try {
    const result = await action();
    aiStatus('done', `${label} finished`);
    aiTask(key, 'done', 'ready');
    return result;
  } catch (error) {
    aiStatus('failed', `${label} failed: ${error.message}`);
    aiTask(key, 'failed', `failed: ${error.message}`);
    throw error;
  }
}

function announce(message) {
  announcer.textContent = '';
  window.setTimeout(() => { announcer.textContent = message; }, 10);
}

function cropLabel(kind) {
  return {
    original: 'Original',
    manual: 'Your crop',
    'subject-in-scene': 'Subject in scene',
    'tighter-subject': 'Tighter subject',
    'centered-fallback': 'Centered fallback',
  }[kind] || 'Crop option';
}

// The photographer's own rect as a crop card, sized against the source like the server's candidates.
function manualCandidate(rect, aspect, composition) {
  const [width, height] = [composition.source_width, composition.source_height];
  const [left, top] = [Math.round(rect.left * width), Math.round(rect.top * height)];
  const pixelBox = [left, top, left + Math.round(rect.width * width), top + Math.round(rect.height * height)];
  const pixelCount = (pixelBox[2] - left) * (pixelBox[3] - top);  // the server's count, so the 8 MP gate agrees
  const megapixels = pixelCount / 1_000_000;
  return {
    id: 'crop-manual', kind: 'manual', aspect, rect, pixel_box: pixelBox, pixel_count: pixelCount,
    remaining_megapixels: megapixels, rationale: 'drawn by the photographer',
    resolution_warning: pixelCount < 12_000_000 ? `${megapixels.toFixed(6)} MP remaining; below 12 MP` : null,
  };
}

// The outline on the frame view: a fresh drawing, else this frame's saved drawn crop.
function shownCropRect() {
  if (state.manualCrop) return state.manualCrop.rect;
  const active = state.composition?.photo_id === state.selectedId ? state.composition.active_crop : null;
  return active?.candidate_id === 'crop-manual' ? active.rect : null;
}

function cropCard(candidate) {
  const photoId = state.composition.photo_id;
  const card = document.createElement('article');
  card.className = 'crop-card';
  const activeId = state.composition.active_crop?.candidate_id;
  const active = candidate.kind === 'original' ? !activeId : candidate.id === activeId && !(candidate.kind === 'manual' && state.manualCrop);
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
  const aspectLabel = { native: 'Original ratio', free: 'Free' }[candidate.aspect] || candidate.aspect;
  details.textContent = `${aspectLabel} · ${candidate.remaining_megapixels.toFixed(1)} MP remaining`;
  const rationale = document.createElement('p');
  rationale.className = 'quiet';
  rationale.textContent = candidate.rationale || 'Full frame, unchanged.';
  const warning = document.createElement('p');
  warning.className = 'crop-warning';
  warning.textContent = candidate.resolution_warning || '';
  warning.id = `crop-warning-${candidate.id}`;
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
    : candidate.kind === 'manual' ? `Use your crop, ${aspectLabel}` : `Use ${heading.textContent} crop, ${aspectLabel}`);
  action.disabled = state.busy || active;
  if (candidate.kind === 'manual' && candidate.resolution_warning) action.setAttribute('aria-describedby', warning.id);  // your crop is yours; the warning is advice
  action.addEventListener('click', () => applyCrop(photoId, candidate));
  card.append(heading, viewport, details, rationale, warning, marker, action);
  return card;
}

function renderComposition() {
  const primary = document.querySelector('#primary-crops');
  const alternatives = document.querySelector('#alternative-crops');
  primary.replaceChildren();
  alternatives.replaceChildren();
  const active = state.composition.active_crop;
  const drawn = state.manualCrop ?? (active?.candidate_id === 'crop-manual' ? active : null);
  if (drawn) primary.append(cropCard(manualCandidate(drawn.rect, drawn.aspect, state.composition)));
  if (!cropDrag && loupeViewport.children[0]?.naturalWidth) drawCropOverlay(shownCropRect());  // before the image loads, its load draws it
  state.composition.candidates.forEach(candidate => {
    (candidate.revealable ? alternatives : primary).append(cropCard(candidate));
  });
  document.querySelector('#more-crop-shapes').hidden = alternatives.children.length === 0;
  document.querySelector('#compose-empty').hidden = true;
  document.querySelector('#compose-workspace').hidden = false;
  document.querySelector('#ask-composition-ai').disabled = state.busy || aiRunning(`composition:${state.composition.photo_id}`) || !roleReady('review');
}

// The stored or fresh review, phrased the same way; a review naming a candidate no longer offered says nothing.
function showCompositionReview(review) {
  const candidate = review && state.composition?.candidates.find(item => item.id === review.candidate_id);
  state.recommendedCropId = candidate ? review.candidate_id : null;
  const advice = candidate ? `${cropLabel(candidate.kind)} recommended (${Math.round(review.confidence * 100)}% confidence): ${review.rationale}` : '';
  document.querySelector('#composition-advice').textContent = advice;
  return advice;
}

async function loadComposition() {
  const requestId = ++state.compositionRequest;
  state.reviewRequest += 1;
  const photoId = state.selectedId;
  state.composition = null;
  state.recommendedCropId = null;
  document.querySelector('#composition-advice').textContent = '';
  document.querySelector('#compose-workspace').hidden = true;
  document.querySelector('#compose-status').textContent = 'Loading crop choices…';
  aiTaskLine(`composition:${photoId}`, document.querySelector('#compose-task'));
  try {
    const composition = await request(`/api/photos/${photoId}/crops`);
    if (requestId !== state.compositionRequest || state.selectedId !== photoId) return;
    state.composition = composition;
    showCompositionReview(composition.review);
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
    const result = candidate.kind === 'original'
      ? await post(`/api/photos/${photoId}/crop/clear`)
      : await post(`/api/photos/${photoId}/crop`, candidate.kind === 'manual'
        ? { candidate_id: candidate.id, rect: candidate.rect, aspect: candidate.aspect }
        : { candidate_id: candidate.id });
    const photo = photoById(photoId);
    if (photo && result.preview_url) {
      photo.preview_url = result.preview_url;  // the contact-sheet thumb now shows the crop export will bake in
      refreshFrame(photoId);
    }
    if (candidate.kind === 'manual' && state.manualCrop?.rect === candidate.rect) state.manualCrop = null;  // saved: the card and outline now come from active_crop; a newer drawing stays
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
  const key = `composition:${photoId}`;
  document.querySelector('#ask-composition-ai').disabled = true;
  document.querySelector('#compose-status').textContent = 'Asking the loaded local vision model…';
  // The server stores the review whether or not the frame is still shown; it is applied here only if it is.
  const current = () => requestId === state.reviewRequest && compositionRequest === state.compositionRequest && state.selectedId === photoId;
  try {
    const review = await aiCall('Composition review', () => post(`/api/photos/${photoId}/composition-review`), key);
    if (!current()) return;
    const advice = showCompositionReview(review);
    if (!advice) {
      document.querySelector('#compose-status').textContent = 'Crop choices changed while the local AI was reviewing them. Reloading current choices…';
      announce('Crop choices changed. Reloading current choices.');
      await loadComposition();
      return;
    }
    renderComposition();
    document.querySelector('#compose-status').textContent = 'Local AI advice is ready. It has not changed your crop.';
    announce(advice);
  } catch (error) {
    if (!current()) return;
    const message = `Local AI advice is unavailable: ${error.message}. Manual crop choices still work.`;
    document.querySelector('#compose-status').textContent = message;
    announce(message);
  } finally {
    if (state.composition?.photo_id === state.selectedId) renderComposition();  // re-enables Ask for the frame shown
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

function startTime(group, withDate = false) {
  if (!group.started_at) return null;
  const date = new Date(group.started_at);
  return withDate ? date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function batchText(group, index) {
  const time = startTime(group);
  return `Burst ${index + 1} · ${frameCount(group.frame_count)}${time ? ` · ${time}` : ''}${allRejected(group) ? ` · ${NO_KEEPER}` : ''}`;
}

// Tiles, rail thumbs, and review rows show the small tier; loupe, compare, and crops keep the full preview.
function thumbUrl(photo) {
  return photo.preview_url.replace(/^\/preview\//, '/thumb/');
}

function bestFrame(group) {
  return group.photos.find(photo => photo.id === group.automatic_pick_id) || group.photos[0] || null;
}

// Scrolling never moves keyboard focus; reduced motion makes every scroll instant.
function scrollMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
}

function scrollToGroup(groupId) {
  const section = document.querySelector(`[data-burst="${groupId}"]`);
  section?.scrollIntoView({ behavior: scrollMotion(), block: 'start' });
  return Boolean(section);
}

function setMergeSelection(groupId, selected) {
  state.mergeSelection[selected ? 'add' : 'delete'](groupId);
  updateWorkflow();
}

function mergeCheckbox(group, groupIndex, className) {
  const select = document.createElement('input');
  select.type = 'checkbox';
  select.className = className;
  select.dataset.mergeGroup = group.id;
  select.checked = state.mergeSelection.has(group.id);
  select.setAttribute('aria-label', `Select ${batchText(group, groupIndex)} for merging`);
  select.addEventListener('change', () => setMergeSelection(group.id, select.checked));
  return select;
}

function selectGroup(group) {
  const first = group.photos[0];
  if (first) return inspect(first);
  // An all-rejected burst with its frames hidden can still be selected, e.g. to remove it.
  state.selectedId = null;
  state.selectedGroupId = group.id;
  inspection.textContent = NO_KEEPER;
  updateWorkflow();
  return Promise.resolve();
}

function burstSection(group, groupIndex, frames) {
  const section = document.createElement('section');
  section.className = 'burst';
  section.dataset.burst = group.id;
  section.setAttribute('aria-labelledby', `burst-${group.id}-heading`);
  const header = document.createElement('div');
  header.className = 'burst-header';
  const heading = document.createElement('h3');
  heading.id = `burst-${group.id}-heading`;
  heading.textContent = `Burst ${groupIndex + 1}`;
  const facts = document.createElement('span');
  facts.className = 'quiet';
  facts.textContent = [frameCount(group.frame_count), startTime(group, true), bestFrame(group) ? `best ${bestFrame(group).name}` : null].filter(Boolean).join(' · ');
  const selected = document.createElement('span');
  selected.className = 'burst-selected';
  const toggle = document.createElement('label');
  toggle.className = 'toggle';
  const text = document.createElement('span');
  text.textContent = 'Select for merge';
  toggle.append(mergeCheckbox(group, groupIndex, 'burst-select'), text);
  header.append(heading, facts, selected, toggle);  // removal and stacking live in the Clear and Astro tabs for the selected burst
  if (group.astro_cue) {  // advisory text from stored triage; it never switches the tab
    const cue = document.createElement('span');
    cue.className = 'astro-cue';
    cue.textContent = 'Looks like astro';
    header.append(cue);
  }
  const grid = document.createElement('div');
  grid.className = 'burst-frames';
  grid.append(...frames);
  section.append(header, grid);
  return section;
}

// Same rule as Library.keeper_ids: not rejected, and flagged or the group's best frame.
function isKeeper(photo, group) {
  return photo.decision !== 'reject' && (photo.decision === 'flag' || photo.id === group.automatic_pick_id);
}

function keepersIn(group) {
  return group.photos.filter(photo => isKeeper(photo, group)).length;
}

// The one decision path for the inspector and the keyboard: field is 'decision', 'rating', or 'pick'.
// focusAfter (inspector only) returns focus to the re-rendered control; keyboard focus stays on the frame.
function cullingBlockedText() {
  return state.acting ? 'Wait for the current change to finish.' : `Culling is paused while ${(JOB_NAMES[state.job.kind] || 'a job').toLowerCase()} runs.`;
}

// Applies a server-confirmed decision to the cards, counts, rail, and inspector in place, without load().
function recordDecision(photo, result) {
  const previous = photo.decision;
  const owner = state.groups.find(item => item.photos.includes(photo));
  const keepersBefore = owner ? keepersIn(owner) : 0;
  photo.decision = result.decision;
  photo.rating = result.rating;
  const delta = (result.decision === 'reject') - (previous === 'reject');
  state.rejectedCount += delta;
  const groupIndex = state.groups.findIndex(item => item.id === result.group_id);
  const group = state.groups[groupIndex];
  if (group) {
    group.automatic_pick_id = result.automatic_pick_id;
    group.rejected_count += delta;
    // ponytail: library-wide count adjusted by the visible burst; exact unfiltered, may drift under a filter until the next load.
    if (group === owner) state.keeperCount += keepersIn(group) - keepersBefore;
    refreshFrames(group);
    refreshPanes(photo.id);
    const batch = document.querySelectorAll('.batch-button')[groupIndex];
    if (batch) batch.textContent = batchText(group, groupIndex);
    refreshDays();
  }
  if (state.selectedId === photo.id) renderInspector(photo);
}

async function decide(photo, field, value, focusAfter = null) {
  if (state.cullingBlocked) return announce(cullingBlockedText());
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
    recordDecision(photo, result);
    const name = photo.name.replace(/\.[^.]+$/, '');
    announce(field === 'pick' ? `${photo.name} is now your best frame.`
      : field === 'rating' ? `Rating ${result.rating} set for ${name}` : `${name} ${DECISION_ANNOUNCEMENTS[result.decision]}`);
    if (state.selectedId === photo.id && focusAfter) document.querySelector(focusAfter)?.focus();
  } catch (error) {
    announce(error.message);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
}

// Keep one frame: pick it as best (which clears its own rejection), then reject every other unrejected frame of its burst.
// ponytail: one POST per frame, in order; a mid-way failure leaves the burst partly rejected and is announced.
// S: the frame leaves its burst for one of its own; the server regroups, so the sheet reloads and selection follows the frame.
async function splitFrame(photo) {
  if (state.cullingBlocked) return announce(cullingBlockedText());
  const group = state.groups.find(item => item.photos.includes(photo));
  if (!group) return;
  if (group.photos.length < 2) return announce(`${photo.name} is already in a burst of its own`);
  state.busy = true;
  updateWorkflow();
  try {
    const result = await post(`/api/photos/${photo.id}/split`);
    state.selectedId = photo.id;
    state.selectedGroupId = result.group_id;
    await load();
    focusFrame(photo.id);
    scrollToGroup(result.group_id);
    announce(`${photo.name} moved to a burst of its own`);
  } catch (error) {
    announce(error.message);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
}

async function keepOnly(photo) {
  if (state.cullingBlocked) return announce(cullingBlockedText());
  const group = state.groups.find(item => item.photos.includes(photo));
  if (!group) return;
  const others = group.photos.filter(item => item !== photo && item.decision !== 'reject');
  state.busy = true;
  updateWorkflow();
  try {
    await post(`/api/photos/${photo.id}/pick`);
    recordDecision(photo, { decision: photo.decision === 'reject' ? 'none' : photo.decision, rating: photo.rating, group_id: group.id, automatic_pick_id: photo.id });
    for (const other of others) recordDecision(other, await post(`/api/photos/${other.id}/decision`, { decision: 'reject' }));
    if (state.selectedId === photo.id) renderInspector(photo);  // the burst changed under the kept frame
    announce(`${photo.name} kept as best; ${others.length} other frame${others.length === 1 ? '' : 's'} rejected`);
  } catch (error) {
    announce(error.message);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
}

function ratingSelect(select) {
  for (let value = 0; value <= 5; value += 1) {
    const option = document.createElement('option');
    option.value = String(value);
    option.textContent = value ? `${value} star${value === 1 ? '' : 's'}` : 'No rating';
    select.append(option);
  }
  return select;
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
    button.disabled = state.cullingBlocked;  // a render between updateWorkflow calls starts in the right state
    button.textContent = label;
    button.addEventListener('click', () => decide(photo, 'decision', value, `[data-decision-control="${value}"]`));
    buttons.append(button);
  }
  const label = document.createElement('label');
  label.htmlFor = 'rating';
  label.textContent = 'Rating';
  const rating = ratingSelect(document.createElement('select'));
  rating.id = 'rating';
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
  try { localStorage.setItem('lumen-selected', String(photo.id)); } catch { /* per-visit only */ }
  state.selectedGroupId = state.groups.find(group => group.photos.some(item => item.id === photo.id))?.id ?? null;
  updateWorkflow();
  document.querySelectorAll('.frame').forEach(button => button.setAttribute('aria-pressed', String(Number(button.dataset.id) === photo.id)));
  const groupId = state.selectedGroupId;
  const critiqueLoad = groupId !== null && state.critique?.groupId !== groupId
    ? request(`/api/groups/${groupId}/critique`).then(payload => payload.critique, () => null) : null;
  const details = await request(`/api/photos/${photo.id}`);
  const critique = critiqueLoad && await critiqueLoad;
  if (state.selectedId !== photo.id) return;
  if (critiqueLoad) state.critique = { groupId, data: critique };
  state.details = details;
  renderInspector(photo);
  fillDevelop(photo);
}

// Plain-language reason for the frame's place in its burst, from the same numbers the meters show.
function rankingExplanation(photo, group) {
  const pct = value => `${Math.round(Math.max(0, Math.min(1, value || 0)) * 100)}%`;
  const highest = key => group.photos.every(item => item.id === photo.id || !(Number(item[key]) > Number(photo[key])));
  const part = (label, key) => `${label} ${pct(photo[key])}${group.photos.length > 1 && highest(key) ? ' (highest)' : ''}`;
  const rank = group.photos.findIndex(item => item.id === photo.id) + 1;
  const paragraph = document.createElement('p');
  paragraph.className = 'ranking';
  paragraph.textContent = `Ranked ${rank} of ${group.photos.length} in this burst: ${part('sharpness', 'sharpness')}, ${part('exposure', 'exposure')}, overall ${pct(photo.total_score)}. Overall weighs sharpness 50% and exposure 25%, plus faces and eyes 25% when detected.`;
  return paragraph;
}

// Advisory only: the stored or fresh suggestion is shown; the pick changes only through "Use this pick".
function burstCritique(photo, group) {
  if (group.photos.filter(item => item.decision !== 'reject').length < 2) return null;
  const block = document.createElement('div');
  block.id = 'burst-critique';
  const ask = document.createElement('button');
  ask.type = 'button';
  ask.className = 'secondary ai';
  ask.id = 'ask-burst-ai';
  ask.setAttribute('aria-describedby', 'frame-ai-availability');
  const taskKey = `critique:${group.id}`;
  const running = aiRunning(taskKey);
  ask.textContent = running ? 'Asking local AI…' : 'Pick the best frame';
  ask.disabled = running || !roleReady('review');
  if (running) ask.setAttribute('aria-busy', 'true');
  ask.addEventListener('click', async () => {
    ask.disabled = true;
    ask.setAttribute('aria-busy', 'true');  // the status bar is under the frame view, so the button shows the wait itself
    ask.textContent = 'Asking local AI…';
    announce('Asking local AI to compare this burst');
    let data = null;
    try {
      data = await aiCall('Burst critique', () => post(`/api/groups/${group.id}/critique`), taskKey);
      if (state.selectedGroupId === group.id || state.critique?.groupId === group.id) state.critique = { groupId: group.id, data };
    } catch (error) {
      announce(error.message);
    }
    // The photographer may have moved on: a frame of this burst still shown is redrawn with the answer, elsewhere nothing moves.
    const shown = state.selectedGroupId === group.id ? photoById(state.selectedId) : null;
    if (!shown) return;
    renderInspector(shown);
    if (data) announce(document.querySelector('#burst-advice')?.textContent || `Local AI suggests ${data.name}`);
  });
  block.append(aiTaskLine(taskKey), ask);
  const data = state.critique?.groupId === group.id ? state.critique.data : null;
  const index = data ? group.photos.findIndex(item => item.id === data.photo_id) : -1;
  if (index >= 0) {
    const suggested = group.photos[index];
    const advice = document.createElement('p');
    advice.id = 'burst-advice';
    const rejected = suggested.decision === 'reject';
    advice.textContent = `Local AI suggests frame ${index + 1} (${data.name}): ${data.rationale} (confidence ${Math.round(data.confidence * 100)}%)${rejected ? ' (frame is rejected)' : ''}`;
    block.append(advice);
    if (rejected) return block;
    const use = document.createElement('button');
    use.type = 'button';
    use.className = 'secondary';
    use.id = 'use-burst-pick';
    use.textContent = 'Use this pick';
    use.addEventListener('click', async () => {
      await decide(suggested, 'pick', null, `.frame[data-id="${suggested.id}"]`);
      if (openView?.id !== 'loupe' || group.automatic_pick_id !== suggested.id || state.selectedId === suggested.id) return;
      focusFrame(suggested.id, false);  // the frame view follows the pick, as a ] step would
      showFrame(suggested);
      viewOrigin = suggested.id;
      flashFrameView();
      announce(`${suggested.name} is now your best frame.`);
    });
    block.append(use);
  }
  return block;
}

// Camera GPS fix as a sentence plus a link the photographer can open; Lumen itself never contacts a map service.
function locationLine(photo, record) {
  const line = document.createElement('p');
  line.id = 'location';
  line.className = 'quiet';
  const place = record?.place ? `Place: ${[record.place, record.place_country].filter(Boolean).join(', ')}` : '';
  const terrain = (record?.terrain || []).join(', ');
  const named = [place, terrain].filter(Boolean).join(' · ');
  if (record?.latitude == null || record?.longitude == null) {
    line.textContent = named || 'No location in the file';  // a place set by hand needs no GPS fix
    return [line, ...placeEditor(photo, record), ...positionEditor(photo, record)];
  }
  const degrees = (value, positive, negative) => `${Math.abs(value).toFixed(4)}° ${value < 0 ? negative : positive}`;
  line.textContent = `${named ? `${named} · ` : 'Location: '}${degrees(record.latitude, 'N', 'S')}, ${degrees(record.longitude, 'E', 'W')}${record.altitude == null ? '' : ` · ${Math.round(record.altitude)} m`}`;
  const link = document.createElement('a');
  link.href = `https://maps.apple.com/?ll=${record.latitude},${record.longitude}&q=${encodeURIComponent(photo.name)}`;
  link.target = '_blank';
  link.rel = 'noopener';
  link.textContent = 'Open in Apple Maps';
  const onMap = document.createElement('a');
  onMap.href = '#map';
  onMap.id = 'show-on-map';
  onMap.textContent = 'Show on map';
  onMap.addEventListener('click', event => {
    event.preventDefault();
    showOnMap(photo, record);
  });
  return [line, link, onMap, ...placeEditor(photo, record), ...(record.location_typed ? positionEditor(photo, record) : [])];
}

// The photographer names the place for this frame, its burst, or every frame inside a radius they set; remembering adds it to the local index at this GPS fix.
function placeEditor(photo, record) {
  const edit = document.createElement('button');
  edit.type = 'button';
  edit.id = 'edit-place';
  edit.className = 'secondary';
  edit.textContent = 'Edit place';
  edit.setAttribute('aria-controls', 'place-form');
  edit.setAttribute('aria-expanded', 'false');
  const form = document.createElement('form');
  form.id = 'place-form';
  form.hidden = true;
  const field = (id, text, value, maxLength) => {
    const label = document.createElement('label');
    label.htmlFor = id;
    label.textContent = text;
    const input = document.createElement('input');
    input.id = id;
    input.autocomplete = 'off';
    input.value = value || '';
    input.maxLength = maxLength;  // the server's limits
    form.append(label, input);
    return input;
  };
  const name = field('place-name', 'Place name', record?.place, 80);
  const country = field('place-country', 'Country', record?.place_country, 60);
  const check = (id, text, checked = true) => {
    const label = document.createElement('label');
    label.className = 'check';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.id = id;
    input.checked = checked;
    label.append(input, ` ${text}`);
    form.append(label);
    return input;
  };
  const burst = check('place-burst', 'Apply to whole burst');
  const nearby = check('place-nearby', 'Apply to every frame within a radius', false);
  const radiusRow = document.createElement('div');
  radiusRow.className = 'place-radius';
  const radiusLabel = document.createElement('label');
  radiusLabel.htmlFor = 'place-radius';
  radiusLabel.textContent = 'Radius';
  const radius = document.createElement('input');
  radius.type = 'range';
  radius.id = 'place-radius';
  radius.min = 1;
  radius.max = 20;
  radius.step = 1;
  radius.value = 5;
  const reading = document.createElement('output');
  reading.id = 'place-radius-km';
  reading.htmlFor = 'place-radius';
  const showRadius = () => {
    reading.textContent = `${radius.value} km`;
    radius.setAttribute('aria-valuetext', `${radius.value} kilometres`);
  };
  showRadius();
  radius.addEventListener('input', showRadius);
  radiusRow.append(radiusLabel, radius, reading);
  form.append(radiusRow);
  const followNearby = () => { radius.disabled = !nearby.checked; };  // the radius only means something for that scope
  followNearby();
  nearby.addEventListener('change', followNearby);
  const day = check('place-day', "Fill this day's frames that have no GPS and no place", false);  // a phone left off: the fix is missing, the date is not
  const remember = check('place-remember', 'Remember for nearby frames');
  if (record?.latitude == null || record?.longitude == null) {
    const reason = document.createElement('span');
    reason.id = 'place-no-fix';
    reason.className = 'quiet';
    reason.textContent = 'no GPS fix';
    for (const input of [nearby, remember]) {  // both need this frame's fix
      input.checked = false;
      input.disabled = true;
      input.setAttribute('aria-describedby', reason.id);
    }
    followNearby();
    form.append(reason);
  }
  const actions = document.createElement('div');
  actions.className = 'actions';
  const save = document.createElement('button');
  save.type = 'submit';
  save.textContent = 'Save place';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.id = 'place-cancel';
  cancel.className = 'secondary';
  cancel.textContent = 'Cancel';
  actions.append(save, cancel);
  form.append(actions);
  const show = open => {
    form.hidden = !open;
    edit.setAttribute('aria-expanded', String(open));
    (open ? name : edit).focus();
  };
  edit.addEventListener('click', () => show(form.hidden));
  cancel.addEventListener('click', () => show(false));
  form.addEventListener('submit', event => {
    event.preventDefault();
    const scope = nearby.checked ? 'nearby' : day.checked ? 'day' : burst.checked ? 'burst' : 'frame';  // the wider choice wins
    return savePlace(photo, { name: name.value, country: country.value, scope, remember: remember.checked, radius_km: Number(radius.value) });
  });
  if (state.placeFocus) { state.placeFocus = false; window.setTimeout(() => edit.focus(), 0); }
  return [edit, form];
}

// A position typed from a ship's display or a map, for frames the camera left without a fix; a camera fix is never
// replaced, but a typed one can be corrected.
function positionEditor(photo, record) {
  const add = document.createElement('button');
  add.type = 'button';
  add.id = 'add-position';
  add.className = 'secondary';
  const editing = Boolean(record?.location_typed);  // read now: saving updates the record
  add.textContent = editing ? 'Edit GPS position' : 'Add GPS position';
  add.setAttribute('aria-controls', 'position-form');
  add.setAttribute('aria-expanded', 'false');
  const form = document.createElement('form');
  form.id = 'position-form';
  form.hidden = true;
  const label = document.createElement('label');
  label.htmlFor = 'position-text';
  label.textContent = 'Position';
  const input = document.createElement('input');
  input.id = 'position-text';
  input.autocomplete = 'off';
  input.maxLength = 80;  // the server's limit
  input.value = editing ? `${record.latitude}, ${record.longitude}` : '';
  input.placeholder = "33°55.2'S 18°25.2'E or -33.92, 18.42";
  form.append(label, input);
  const check = (id, text, checked) => {
    const box = document.createElement('label');
    box.className = 'check';
    const control = document.createElement('input');
    control.type = 'checkbox';
    control.id = id;
    control.checked = checked;
    box.append(control, ` ${text}`);
    form.append(box);
    return control;
  };
  const burst = check('position-burst', 'Apply to the whole burst', true);
  const day = check('position-day', "Apply to this day's frames that have no GPS", false);
  const actions = document.createElement('div');
  actions.className = 'actions';
  const save = document.createElement('button');
  save.type = 'submit';
  save.textContent = 'Save position';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'secondary';
  cancel.textContent = 'Cancel';
  actions.append(save, cancel);
  form.append(actions);
  const show = open => {
    form.hidden = !open;
    add.setAttribute('aria-expanded', String(open));
    (open ? input : add).focus();
  };
  add.addEventListener('click', () => show(form.hidden));
  cancel.addEventListener('click', () => show(false));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      const result = await post(`/api/photos/${photo.id}/location`, { position: input.value, scope: day.checked ? 'day' : burst.checked ? 'burst' : 'frame' });
      const fix = { latitude: result.latitude, longitude: result.longitude, altitude: null, location_typed: true };
      for (const item of state.groups.flatMap(group => group.photos)) if (result.photo_ids.includes(item.id)) Object.assign(item, fix);
      if (state.selectedId === photo.id) {
        Object.assign(state.details.photo, fix, { place: result.place, place_country: result.country });
        state.placeFocus = true;
        renderInspector(photo);
      }
      const count = result.photo_ids.length;
      announce(`GPS position ${editing ? 'changed on' : 'added to'} ${count} frame${count === 1 ? '' : 's'}`);
    } catch (error) {
      announce(error.message);  // the form stays open with what was typed
    }
  });
  return [add, form];
}

async function savePlace(photo, payload) {
  try {
    const result = await post(`/api/photos/${photo.id}/place`, payload);
    const place = { place: result.name, place_country: result.country };
    for (const item of state.groups.flatMap(group => group.photos)) if (result.photo_ids.includes(item.id)) Object.assign(item, place);
    if (state.selectedId === photo.id) {
      Object.assign(state.details.photo, place);
      state.placeFocus = true;
      renderInspector(photo);
    }
    const count = result.photo_ids.length;
    announce(`Place set to ${result.name} on ${count} frame${count === 1 ? '' : 's'}${result.remembered ? ', remembered' : ''}`);
  } catch (error) {
    announce(error.message);  // the form stays open with what was typed
  }
}

function renderInspector(photo) {
  const details = state.details;
  inspection.replaceChildren();
  const verdict = document.createElement('p');
  verdict.className = 'quiet';
  const group = state.groups.find(item => item.photos.includes(photo));
  verdict.textContent = group && allRejected(group) ? NO_KEEPER
    : photo.id === photo.groupPick ? 'Best measured frame in this group.' : 'Alternate frame; compare its component scores.';
  inspection.append(verdict);
  if (photo.matched?.length) {
    const reasons = document.createElement('p');
    reasons.id = 'match-reasons';
    reasons.textContent = `Matches: ${photo.matched.join(', ')}`;
    inspection.append(reasons);
  }
  if (group) inspection.append(rankingExplanation(photo, group));
  inspection.append(...locationLine(photo, details.photo));
  const critique = group && burstCritique(photo, group);
  if (critique) inspection.append(critique);
  inspection.append(decisionControls(photo));
  for (const text of details.warnings || []) {
    const warning = document.createElement('p');
    warning.className = 'warning';
    warning.textContent = `Warning: ${text}`;
    inspection.append(warning);
  }
  inspection.append(metric('Overall', photo.total_score), metric('Sharpness', details.metrics.sharpness), metric('Exposure', details.metrics.exposure));
  if (details.metrics.eye_quality !== null) inspection.append(metric('Eyes', details.metrics.eye_quality));
  if (details.tags?.data) {
    const description = document.createElement('p');
    description.textContent = details.tags.data.description;
    inspection.append(description);
  }
  inspection.append(...keywordLists(photo, details.tags?.data || {}));  // an untagged frame still takes typed keywords
  inspection.append(captionControls(photo, details.tags?.data || {}));
  const actions = document.createElement('div');
  actions.className = 'actions';
  if (photo.id !== photo.groupPick || photo.decision === 'reject') {  // Choosing a rejected frame clears its rejection.
    const choose = document.createElement('button');
    choose.type = 'button';
    choose.textContent = 'Choose as best';
    choose.addEventListener('click', () => decide(photo, 'pick', null, `.frame[data-id="${photo.id}"]`));
    actions.append(choose);
  }
  if (group && group.photos.some(item => item !== photo && item.decision !== 'reject')) {
    const keep = document.createElement('button');
    keep.type = 'button';
    keep.className = 'secondary';
    keep.textContent = 'Keep this, reject the rest';
    keep.addEventListener('click', () => keepOnly(photo));
    actions.append(keep);
  }
  const tag = document.createElement('button');
  tag.type = 'button';
  tag.className = 'secondary ai';
  tag.setAttribute('aria-describedby', 'frame-ai-availability');
  tag.textContent = 'Tag this frame';
  tag.dataset.tagPhoto = 'true';
  const tagKey = `tag:${photo.id}`;
  tag.disabled = aiRunning(tagKey) || !roleReady('fast');
  tag.addEventListener('click', async () => {
    tag.disabled = true;
    try {
      await aiCall(`Tagging ${photo.name}`, () => request(`/api/photos/${photo.id}/tag`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }), tagKey);
      announce(`Tags added to ${photo.name}`);
    } catch (error) {
      announce(error.message);
    }
    // The new tags and Ask enabled again, for this frame only; not inspect(), whose fillDevelop would drop unsaved develop handles.
    if (state.selectedId !== photo.id) return;
    const details = await request(`/api/photos/${photo.id}`).catch(() => null);
    if (state.selectedId !== photo.id) return;
    if (details) state.details = details;
    renderInspector(photo);
  });
  actions.append(tag, aiTaskLine(tagKey));
  inspection.append(actions);
  keepFrameViewFocus();
}

// Advisory: a local AI draft is stored as the model's and shown for editing; only the Share export uses it.
function captionControls(photo, data) {
  const block = document.createElement('div');
  block.id = 'caption-controls';
  const heading = document.createElement('h4');
  heading.textContent = 'Caption and alt text';
  const note = document.createElement('p');
  note.className = 'quiet';
  note.id = 'caption-note';
  note.textContent = 'Used only by the Share and Web exports. A local AI draft is a suggestion: edit it, then save.';
  block.append(aiTaskLine(`caption:${photo.id}`), heading, note);
  const fields = [['caption-input', 'Caption', data.caption], ['alt-input', 'Alt text', data.alt_text]].map(([id, text, value]) => {
    const label = document.createElement('label');
    label.htmlFor = id;
    label.textContent = text;
    const input = document.createElement('input');
    input.id = id;
    input.autocomplete = 'off';
    input.value = value || '';
    input.setAttribute('aria-describedby', note.id);
    block.append(label, input);
    return input;
  });
  const [caption, alt] = fields;
  caption.maxLength = 200;  // CAPTION_MAX in omlx.py; the server enforces the same limit.
  alt.maxLength = 125;  // ALT_TEXT_MAX in omlx.py.
  const actions = document.createElement('div');
  actions.className = 'actions';
  const save = document.createElement('button');
  save.type = 'button';
  save.id = 'save-caption';
  save.textContent = 'Save caption';
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const saved = await post(`/api/photos/${photo.id}/caption/edit`, { caption: caption.value, alt_text: alt.value });
      Object.assign(data, { caption: saved.caption, alt_text: saved.alt_text });
      announce(`Caption saved for ${photo.name}`);
    } catch (error) {
      announce(error.message);
    } finally {
      save.disabled = false;
    }
  });
  const ask = document.createElement('button');
  ask.type = 'button';
  ask.className = 'secondary ai';
  ask.id = 'ask-caption-ai';
  ask.setAttribute('aria-describedby', 'frame-ai-availability');
  ask.textContent = 'Draft a caption';
  const captionKey = `caption:${photo.id}`;
  ask.disabled = aiRunning(captionKey) || !roleReady('review');
  ask.addEventListener('click', async () => {
    ask.disabled = true;
    announce(`Asking local AI to caption ${photo.name}`);
    try {
      const drafted = await aiCall(`Caption for ${photo.name}`, () => post(`/api/photos/${photo.id}/caption`), captionKey);
      caption.value = drafted.caption || '';
      alt.value = drafted.alt_text || '';
      Object.assign(data, { caption: drafted.caption, alt_text: drafted.alt_text });
      if (state.selectedId === photo.id && state.details) {  // so a redraw, now or later, shows the draft
        state.details.tags = { ...state.details.tags, data: { ...state.details.tags?.data, caption: drafted.caption, alt_text: drafted.alt_text } };
      }
      announce(`Local AI caption written for ${photo.name}; edit and save to change it`);
    } catch (error) {
      announce(error.message);
    }
    // Culling went on meanwhile: if the inspector was redrawn under this block, redraw it again for the frame shown.
    if (inspection.contains(block)) ask.disabled = !roleReady('review');
    else if (state.selectedId === photo.id) renderInspector(photo);
  });
  actions.append(save, ask);
  block.append(actions);
  return block;
}

// Applied keywords each carry a Remove (undo); suggestions carry an Add. Species ride along as labelled text.
function keywordLists(photo, data) {
  const subjects = data.subjects || [];
  const applied = new Set(subjects.map(word => word.toLowerCase()));
  const lists = [];
  for (const [id, label, words, action] of [['keywords', 'Keywords', subjects, 'remove'], ['suggested-keywords', 'Suggested keywords', data.suggested || [], 'add']]) {
    const heading = document.createElement('h4');
    heading.id = `${id}-heading`;
    heading.textContent = label;
    const list = document.createElement('ul');
    list.id = id;
    list.setAttribute('aria-labelledby', heading.id);
    for (const word of words) {
      const item = document.createElement('li');
      const text = document.createElement('span');
      const species = action === 'add' && (data.species || []).find(entry => entry.name.toLowerCase() === word.toLowerCase());
      text.textContent = species ? `${word} (species, ${Math.round(species.confidence * 100)}%)` : word;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'secondary';
      button.textContent = action === 'add' ? 'Add' : 'Remove';
      button.setAttribute('aria-label', `${action === 'add' ? 'Add' : 'Remove'} keyword ${word}`);
      button.addEventListener('click', () => changeKeyword(photo, action, word));
      item.append(text, button);
      list.append(item);
      if (state.keywordFocus === `${action}:${word}`) { state.keywordFocus = null; window.setTimeout(() => button.focus(), 0); }
    }
    for (const species of action === 'remove' ? data.species || [] : []) {  // a suggested species is labelled on its Add row
      if (!applied.has(species.name.toLowerCase())) continue;
      const item = document.createElement('li');
      item.textContent = `Species: ${species.name} (${Math.round(species.confidence * 100)}%)`;
      list.append(item);
    }
    lists.push(heading, list);
    if (action === 'remove') lists.push(...typedKeyword(photo, applied));
  }
  return lists;
}

// A word of the photographer's own; the vocabulary datalist offers known words without limiting the choice.
function typedKeyword(photo, applied) {
  const label = document.createElement('label');
  label.htmlFor = 'keyword-input';
  label.textContent = 'Add a keyword';
  const row = document.createElement('div');
  row.className = 'input-row';
  const input = document.createElement('input');
  input.id = 'keyword-input';
  input.autocomplete = 'off';
  input.maxLength = 40;  // the server's keyword limit
  input.setAttribute('list', 'vocabulary-words');
  const add = document.createElement('button');
  add.type = 'button';
  add.id = 'keyword-add';
  add.className = 'secondary';
  add.textContent = 'Add';
  const submit = () => addTypedKeyword(photo, input.value, applied);
  add.addEventListener('click', submit);
  input.addEventListener('keydown', event => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    return submit();
  });
  row.append(input, add);
  if (state.keywordFocus === 'input') { state.keywordFocus = null; window.setTimeout(() => input.focus(), 0); }
  return [label, row];
}

async function addTypedKeyword(photo, word, applied) {
  word = word.trim();
  if (!word) return;
  if (applied.has(word.toLowerCase())) return announce(`Keyword ${word} is already on ${photo.name}`);  // the server refuses it too
  return changeKeyword(photo, 'add', word, 'input');
}

async function changeKeyword(photo, action, keyword, focus = `${action === 'add' ? 'remove' : 'add'}:${keyword}`) {
  try {
    const result = await post(`/api/photos/${photo.id}/keywords`, { action, keyword });
    if (action === 'add' && !(state.vocabularyWords ??= []).some(word => word.toLowerCase() === keyword.toLowerCase())) {
      state.vocabularyWords.push(keyword);  // the server learned it; offer it while typing without a reload
      document.querySelector('#vocabulary-words').append(Object.assign(document.createElement('option'), { value: keyword }));
    }
    if (state.selectedId !== photo.id) return;
    state.details.tags ??= { data: {} };  // the first keyword on an untagged frame
    Object.assign(state.details.tags.data, { subjects: result.subjects, suggested: result.suggested });
    state.keywordFocus = focus;  // the undo control, or the typing input
    renderInspector(photo);
    announce(`Keyword ${keyword} ${action === 'add' ? 'added to' : 'removed from'} ${photo.name}`);
  } catch (error) {
    announce(error.message);
  }
}

function fillFrame(button, photo, group) {
  photo.groupPick = group.automatic_pick_id;
  const best = photo.id === group.automatic_pick_id && photo.decision !== 'reject';
  const marks = [DECISION_WORDS[photo.decision], photo.rating ? `Rating ${photo.rating}` : null, state.compare.includes(photo.id) ? 'Compare' : null,
    photo.preview_kind === 'refined' ? 'Refined' : null]
    .filter(word => word && word !== DECISION_WORDS.none);
  button.dataset.decision = photo.decision;
  button.setAttribute('aria-label', [`${photo.name}, score ${Math.round(photo.total_score * 100)} percent`, best ? 'best in group' : null, ...marks.map(word => word.toLowerCase())].filter(Boolean).join(', '));
  const image = document.createElement('img');
  image.src = thumbUrl(photo);
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

function sheetColumns() {  // frames sit in per-burst grids; every grid shares one column count
  const grid = document.querySelector('.burst-frames');
  const columns = grid && window.getComputedStyle?.(grid).gridTemplateColumns;
  return columns && columns !== 'none' ? columns.trim().split(/\s+/).length : 1;
}

function toggleCompare(photo) {
  const marked = state.compare.includes(photo.id);
  const changed = marked ? [photo.id] : [photo.id, ...(state.compare.length >= 2 ? [state.compare.shift()] : [])];
  state.compare = marked ? state.compare.filter(id => id !== photo.id) : [...state.compare, photo.id];
  changed.forEach(refreshFrame);
  updateWorkflow();
  announce(marked ? 'Unmarked' : `Marked for compare (${state.compare.length} of 2)`);
}

const CULLING_KEYS = { p: 'pick', r: 'keepOnly', s: 'split', x: 'reject', f: 'flag', u: 'clear', c: 'compare', z: 'full', Enter: 'loupe', ' ': 'loupe', Escape: 'close' };
const CULLING_ACTIONS = {
  pick: photo => decide(photo, 'pick'),
  keepOnly,
  split: splitFrame,
  reject: photo => decide(photo, 'decision', 'reject'),
  flag: photo => decide(photo, 'decision', 'flag'),
  clear: photo => decide(photo, 'decision', 'none'),
  compare: toggleCompare,
  loupe: photo => openLoupe(photo.id),
  close: () => closeView(),
  full: () => openView?.id === 'loupe' && toggleFullSize(),
};

function photoById(id) {
  return state.groups.flatMap(group => group.photos).find(photo => photo.id === id);
}

function handleCullingKey(event) {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  const target = event.target;
  const key = event.key?.length === 1 ? event.key.toLowerCase() : event.key;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target?.tagName)) return;
  if (key === 'Enter' && openView) return;  // no culling meaning in an open view: buttons, links, and summaries keep native activation
  if (key === ' ' && (openView || target?.className !== 'frame')) return;  // Space opens a frame card only; elsewhere it scrolls or activates natively
  // Only frame cards cull on the sheet; in the frame view its own buttons cull too.
  if (target?.tagName === 'BUTTON' && target.className !== 'frame' && openView?.id !== 'loupe') return;
  // Left and Right step too, unless a tab list or the curve editor already used the arrow.
  const step = { '[': -1, ']': 1, ...(event.defaultPrevented ? {} : { ArrowLeft: -1, ArrowRight: 1 }) }[key];
  if (step && openView?.id === 'loupe') { event.preventDefault(); return stepLoupe(step); }
  const ids = Array.from(document.querySelectorAll('.frame'), card => Number(card.dataset.id));
  const index = ids.indexOf(state.focusedId);
  const row = sheetColumns();
  const next = { ArrowLeft: index - 1, ArrowRight: index + 1, ArrowUp: index - row, ArrowDown: index + row, PageUp: index - row, PageDown: index + row, Home: 0, End: ids.length - 1 }[key];
  if (next !== undefined) {
    if (!ids.length || openView) return;  // never move focus behind an open compare or loupe
    event.preventDefault();
    return focusFrame(ids[Math.max(0, Math.min(ids.length - 1, next))]);
  }
  const action = /^[0-5]$/.test(key) ? photo => decide(photo, 'rating', Number(key)) : CULLING_ACTIONS[CULLING_KEYS[key]];
  if (!action) return;
  event.preventDefault();
  const photo = photoById(state.focusedId);
  return photo ? action(photo) : undefined;
}

const PAGE = 200;
let dayHeadings = [];  // { day, summary, groups } per rail <details>, so decisions can refresh keeper counts
let openDays = null;  // days the photographer opened or closed by hand; until then the selected burst's day (else the first) is open

function rememberOpenDays() {
  openDays = new Set(dayHeadings.filter(heading => heading.details.open).map(heading => heading.day));
}

function dayIsOpen(day, index, selectedDay) {
  if (openDays) return openDays.has(day) || day === selectedDay;
  return selectedDay ? day === selectedDay : index === 0;
}

function dayText(day, groups) {
  const frames = groups.reduce((total, group) => total + group.frame_count, 0);
  const keepers = groups.reduce((total, group) => total + keepersIn(group), 0);
  return `${day} · ${frameCount(frames)} · ${keepers} keeper${keepers === 1 ? '' : 's'}`;
}

function refreshDays() {
  dayHeadings.forEach(heading => { heading.summary.textContent = dayText(heading.day, heading.groups); });
}

function pagedPhotos() {
  return state.groups.flatMap(group => group.photos.map(photo => [photo, group]));
}

function announceShowing() {
  const total = pagedPhotos().length;
  announce(`Showing ${Math.min(state.shown, total)} of ${total} frames`);
}

// The rail groups bursts by capture day; the sheet renders PAGE frames at a time.
function renderGroups() {
  sheet.replaceChildren();
  const rail = document.querySelector('#batch-list');
  rail.replaceChildren();
  dayHeadings = [];
  const photos = pagedPhotos();
  const visible = photos.slice(0, state.shown);
  const ids = visible.map(([photo]) => photo.id);
  if (!ids.includes(state.focusedId)) state.focusedId = ids.includes(state.selectedId) ? state.selectedId : ids[0] ?? null;
  state.compare = state.compare.filter(id => photos.some(([photo]) => photo.id === id));
  const dayOf = group => group.started_at?.slice(0, 10) || 'Undated';
  const selectedDay = state.groups.filter(group => group.id === state.selectedGroupId).map(dayOf)[0] ?? null;
  state.groups.forEach((group, groupIndex) => {
    const day = dayOf(group);
    let heading = dayHeadings.at(-1);
    if (heading?.day !== day) {
      const details = document.createElement('details');
      details.className = 'day';
      details.open = dayIsOpen(day, dayHeadings.length, selectedDay);
      const summary = document.createElement('summary');
      summary.addEventListener('click', () => window.setTimeout(rememberOpenDays, 0));  // after the native toggle
      details.append(summary);
      rail.append(details);
      heading = { day, summary, details, groups: [] };
      dayHeadings.push(heading);
    }
    heading.groups.push(group);
    const thumb = document.createElement('img');
    thumb.className = 'batch-thumb';
    thumb.src = bestFrame(group) ? thumbUrl(bestFrame(group)) : '';
    thumb.alt = '';
    thumb.loading = 'lazy';
    const batch = document.createElement('button');
    batch.type = 'button';
    batch.className = 'batch-button';
    batch.textContent = batchText(group, groupIndex);
    batch.setAttribute('aria-pressed', String(group.id === state.selectedGroupId));
    batch.addEventListener('click', () => {  // selects and scrolls the burst into view; focus stays on the rail
      const first = group.photos[0];
      if (first && !frameCard(first.id)) {  // reveal the pages up to this burst
        state.shown = Math.ceil((photos.findIndex(([photo]) => photo === first) + 1) / PAGE) * PAGE;
        state.selectedGroupId = group.id;  // so the rebuilt rail keeps this burst's day open
        renderGroups();
      }
      selectGroup(group).then(() => { if (!first) announce(batchText(group, groupIndex)); }).catch(error => announce(error.message));
      scrollToGroup(group.id);
    });
    const row = document.createElement('div');
    row.className = 'batch-row';
    row.append(thumb, mergeCheckbox(group, groupIndex, 'batch-select'), batch);
    heading.details.append(row);
  });
  refreshDays();
  state.groups.forEach((group, groupIndex) => {  // one section per burst with frames on this page
    const frames = visible.filter(([, owner]) => owner === group).map(([photo]) => renderFrame(photo, group));
    if (frames.length) sheet.append(burstSection(group, groupIndex, frames));
  });
  const remaining = photos.length - visible.length;
  const more = document.querySelector('#show-more');
  more.hidden = remaining <= 0;
  more.textContent = `Show ${Math.min(PAGE, remaining)} more · ${remaining} remaining`;
  if (!photos.length && !location.search) sheet.append(firstRunCard());
  else if (!photos.length) {
    const empty = document.createElement('p');
    empty.className = 'quiet';
    empty.textContent = 'No frames match these filters.';
    sheet.append(empty);
  }
  // A narrowed sheet says how much of the library it shows.
  document.querySelector('#photo-count').textContent = location.search ? `${photos.length} of ${frameCount(state.photoCount)}` : frameCount(photos.length);
  if (!state.groups.length) rail.textContent = location.search ? 'No batches match these filters.' : 'No imported batches yet.';
  updateWorkflow();
}

// An empty, unfiltered sheet shows the way in: each button opens Add photos on the matching source.
function firstRunCard() {
  let hidden = null;
  try { hidden = localStorage.getItem('lumen-welcome-hidden'); } catch { /* per-visit only */ }
  if (hidden === '1') {
    const empty = document.createElement('p');
    empty.className = 'quiet';
    empty.textContent = 'Import a folder to build your first contact sheet.';
    return empty;
  }
  const card = document.createElement('section');
  card.className = 'first-run';
  card.setAttribute('aria-label', 'Get started');
  const mark = document.createElement('img');
  mark.src = '/static/mark.svg';
  mark.alt = '';
  mark.width = 64;
  mark.height = 64;
  const intro = document.createElement('div');
  const title = document.createElement('h2');
  title.textContent = 'Lumen';
  const promise = document.createElement('p');
  promise.className = 'quiet';
  promise.textContent = 'Reads your Sony raws where they are. Originals are never moved, renamed, or changed.';
  intro.append(title, promise);
  const choices = document.createElement('div');
  choices.className = 'first-run-choices';
  [['ingest', 'Photos still on a memory card', 'Ingest makes a verified copy first, so you can format the card.'],
   ['import', 'Photos already on a drive or folder', 'Import adds them in place. Nothing is copied.']].forEach(([source, label, help]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'secondary';
    button.dataset.source = source;
    const strong = document.createElement('strong');
    strong.textContent = label;
    const note = document.createElement('span');
    note.textContent = help;
    button.append(strong, note);
    button.addEventListener('click', () => {
      const radio = addSourceRadios.find(item => item.value === source);
      radio.checked = true;
      radio.dispatchEvent(new Event('change'));
      selectTab(document.querySelector('#add-tab'));
      document.querySelector(source === 'ingest' ? '#browse-ingest-card' : '#browse-path').focus();
    });
    choices.append(button);
  });
  const hint = document.createElement('p');
  hint.className = 'quiet';
  hint.textContent = 'Not sure? Ingest if the card is in the reader. Import if the photos are already backed up.';
  const setup = document.createElement('dl');
  setup.className = 'first-run-setup';
  setup.setAttribute('aria-label', 'Local tools');
  firstRunSetupRows(state.status || {}).forEach(([name, value, ok]) => {
    const term = document.createElement('dt');
    term.textContent = name;
    const detail = document.createElement('dd');
    detail.textContent = value;
    detail.dataset.ok = String(ok);
    setup.append(term, detail);
  });
  const dismiss = document.createElement('label');
  dismiss.className = 'toggle first-run-dismiss';
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.addEventListener('change', () => {
    try { localStorage.setItem('lumen-welcome-hidden', '1'); } catch { /* per-visit only */ }
    renderGroups();
    announce('Welcome card hidden. Add photos is in the tabs above.');
  });
  dismiss.append(box, " Don't show this again");
  card.append(mark, intro, choices, hint, setup, dismiss);
  return card;
}

// The same facts as the header and footer, spelled out once for someone setting up: [label, text, healthy].
function firstRunSetupRows(status) {
  const rows = [];
  const live = status.omlx_live;
  const roles = status.omlx_roles || {};
  const role = name => roles[name] ? `${roles[name].model}${roles[name].loaded ? '' : ' (not loaded)'}` : 'unknown';
  rows.push(live?.online
    ? ['Local AI', `online · review ${role('review')} · fast ${role('fast')}`, Boolean(roles.review?.loaded && roles.fast?.loaded)]
    : ['Local AI', 'offline. Start oMLX with a vision model loaded; culling, import and export work without it', false]);
  const helper = status.raw_developer;
  rows.push(['Raw developer', helper?.available ? 'ready' : 'missing. Run zsh scripts/bootstrap.sh to compile it; raw exports need it', helper?.available === true]);
  Object.entries(status.tools || {}).filter(([name]) => name !== 'omlx' && name !== 'swiftc').forEach(([name, tool]) => {
    const what = { exiftool: 'exports', 'darktable-cli': 'profile exports', 'siril-cli': 'astro stacking' }[name] || name;
    rows.push([name, tool?.available ? `${tool.version || 'ready'}` : `missing. ${tool?.required ? 'Every export needs it' : `Needed only for ${what}`}`, tool?.available === true]);
  });
  return rows;
}

function showMore() {
  const first = state.shown;
  state.shown += PAGE;
  renderGroups();
  const card = document.querySelectorAll('.frame')[first];
  if (card) focusFrame(Number(card.dataset.id));
  announceShowing();
}

// Filter state lives only in the URL query; the field names match the /api/groups parameters.
const FILTER_CONTROLS = {
  date_from: '#filter-from', date_to: '#filter-to', camera: '#filter-camera', min_rating: '#filter-rating', tagged_only: '#filter-tagged',
  export_failed_only: '#filter-export-failed', text: '#filter-text',
};
const FLAG_FILTERS = ['tagged_only', 'export_failed_only'];
const FILTER_DEFAULTS = { min_rating: '0' };
// One status choice stands for keepers_only or a decision; "all" hides rejected frames, as the server does by default.
const STATUSES = { all: {}, keepers: { keepers_only: '1' }, flag: { decision: 'flag' }, none: { decision: 'none' }, reject: { decision: 'reject' } };
const statusRadio = value => document.querySelector(`#status-${value}`);

function filterStatus() {
  return Object.keys(STATUSES).find(value => statusRadio(value).checked) || 'all';
}
let findTimer = null;  // pending debounced apply from #find / #filter-text

function cancelFind() {
  window.clearTimeout(findTimer);
  findTimer = null;
}

function filterQuery() {
  const params = new URLSearchParams();
  Object.entries(FILTER_CONTROLS).forEach(([name, selector]) => {
    const control = document.querySelector(selector);
    const value = FLAG_FILTERS.includes(name) ? (control.checked ? '1' : '') : control.value.trim();
    if (value && value !== FILTER_DEFAULTS[name]) params.set(name, value);
  });
  Object.entries(STATUSES[filterStatus()]).forEach(([name, value]) => params.set(name, value));
  return params.toString();
}

function syncFilters(cameras) {
  const params = new URLSearchParams(location.search);
  const anyCamera = document.createElement('option');
  anyCamera.value = '';
  anyCamera.textContent = 'Any camera';
  document.querySelector('#filter-camera').replaceChildren(anyCamera, ...cameras.map(camera => {
    const option = document.createElement('option');
    option.value = camera;
    option.textContent = camera;
    return option;
  }));
  // A pending find apply owns both text fields, as does a mirror that matches the field being typed in.
  const text = ['#find', '#filter-text'].map(selector => document.querySelector(selector));
  const holdText = findTimer !== null || (text.includes(document.activeElement) && text[0].value === text[1].value);
  Object.entries(FILTER_CONTROLS).forEach(([name, selector]) => {
    const control = document.querySelector(selector);
    if (FLAG_FILTERS.includes(name)) control.checked = params.get(name) === '1';
    else if (!(name === 'text' && holdText)) keepTyping(control, params.get(name) ?? FILTER_DEFAULTS[name] ?? '');
  });
  if (!holdText) keepTyping(text[0], params.get('text') ?? '');
  const status = params.get('keepers_only') === '1' ? 'keepers' : params.get('decision') in STATUSES ? params.get('decision') : 'all';
  Object.keys(STATUSES).forEach(value => { statusRadio(value).checked = value === status; });
}

// A reload never rewrites the field being typed in, nor one that differs from the URL only by spaces.
function keepTyping(control, value) {
  if (control !== document.activeElement && control.value.trim() !== value) control.value = value;
}

async function applyFilters(query = filterQuery()) {
  const previous = location.search;
  history.replaceState(null, '', query ? `?${query}` : location.pathname);
  state.shown = PAGE;
  try {
    await load();
  } catch (error) {  // A rejected filter never sticks in the URL; the form keeps what was typed.
    history.replaceState(null, '', previous || location.pathname);
    throw error;
  }
  announceShowing();
}

// Each model role (review, fast) runs on the full or the fast model; the switch is the server's, so a reload shows what it uses now.
const roleSelects = [...document.querySelectorAll('#role-model-pair select')];
function syncRoleModels(status) {
  const models = Array.isArray(status.review_models) ? status.review_models : [];
  document.querySelector('#role-model-pair').hidden = models.length < 2;
  const loaded = status.omlx_live?.loaded_models || [];
  for (const select of roleSelects) {
    select.replaceChildren(...models.map(model => new Option(loaded.includes(model) ? model : `${model} (not loaded)`, model)));
    select.value = status.omlx_roles?.[select.dataset.role]?.model ?? '';
  }
}
for (const select of roleSelects) {
  select.addEventListener('change', async () => {
    try {
      const { role, model } = await post('/api/role-model', { role: select.dataset.role, model: select.value });
      announce(`${role === 'fast' ? 'Fast' : 'Review'} model is now ${model}.`);
    } catch (error) {
      announce(error.message);
    }
    await load();
  });
}

let resumed = false;
let defaultTabChosen = false;  // only the first load picks the tab a fresh visit opens
async function load() {
  const [status, groups] = await Promise.all([request('/api/status'), request(`/api/groups${location.search}`)]);
  state.status = status;
  state.groups = groups.groups;
  state.mergeSelection = new Set([...state.mergeSelection].filter(id => state.groups.some(group => group.id === id)));
  state.rejectedCount = groups.rejected_count || 0;
  state.photoCount = groups.photo_count;
  state.keeperCount = groups.keeper_count;
  if (!defaultTabChosen) { defaultTabChosen = true; chooseDefaultTab(); }
  syncFilters(groups.cameras);
  const selectedGroup = state.groups.find(group => group.photos.some(photo => photo.id === state.selectedId))
    || (state.selectedId === null ? state.groups.find(group => group.id === state.selectedGroupId) : undefined);
  state.selectedGroupId = selectedGroup?.id ?? null;
  if (!selectedGroup) {
    state.selectedId = null;
    inspection.textContent = 'Select a frame to see why it ranked where it did.';
  }
  // A server older than this page answers without omlx_roles; the sheet must still render, so the status line only reports it.
  // The header reports health only; model names and their load state live in the footer strip.
  const systemStatus = document.querySelector('#system-status');
  const missing = Object.entries(status.tools || {}).filter(([, tool]) => tool?.available === false).map(([name]) => `${name} missing`);
  const health = [status.omlx_live?.online ? 'Local AI online' : 'Local AI offline', ...missing].join(' · ');
  const tools = status.omlx_roles
    ? health
    : `${health} · the running Lumen server is older than this page; restart lumen serve to enable AI tools`;
  // Static files are read from disk on every request while the Python stays as it was
  // imported, so a stale server looks current and fails as whatever validation error comes
  // first. It goes ahead of the tool summary because nothing else here is actionable until
  // it is dealt with.
  const stale = Array.isArray(status.stale) ? status.stale : [];
  systemStatus.textContent = [...stale, tools].join(' · ');
  document.querySelector('#app-version').textContent = status.version ? `v${status.version}` : '';
  if (stale.length) systemStatus.setAttribute('data-stale', '');
  else systemStatus.removeAttribute('data-stale');
  syncRoleModels(status);
  document.querySelector('#export-destination').textContent = status.jpeg_output_dir;
  renderGroups();
  if (!resumed && state.selectedId === null) {  // only a fresh page resumes the frame inspected before the reload
    resumed = true;  // later loads (filters, jobs, cleanup) must not steal focus
    let remembered = null;
    try { remembered = localStorage.getItem('lumen-selected'); } catch { /* per-visit only */ }
    const photos = pagedPhotos();
    const index = remembered === null ? -1 : photos.findIndex(([photo]) => String(photo.id) === remembered);
    if (index >= 0) {
      const [photo, group] = photos[index];
      state.shown = Math.max(state.shown, Math.ceil((index + 1) / PAGE) * PAGE);
      state.selectedGroupId = group.id;  // so the rebuilt rail opens this frame's day
      renderGroups();
      await inspect(photo);
      focusFrame(photo.id);
    } else if (remembered !== null) {
      try { localStorage.removeItem('lumen-selected'); } catch { /* per-visit only */ }
    }
  }
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
  const reviewing = document.querySelector('#review-tab').getAttribute('aria-selected') === 'true';
  await Promise.all([reviewing && loadRejectSuggestions(), loadVocabulary(!reviewing)]);  // the inspector's datalist needs the words on every load
}

// Import is a tab of its own, beside Ingest.
document.querySelector('#import-form').addEventListener('submit', async event => {
  event.preventDefault();
  state.busy = true;
  updateWorkflow();
  document.querySelector('#import-status').textContent = 'Starting import…';
  try {
    const payload = { path: document.querySelector('#import-path').value };
    if (document.querySelector('#import-include-cleared').checked) payload.include_cleared = true;
    watchJob(await post('/api/import', payload));
    document.querySelector('#import-status').textContent = 'Import started. Progress and results appear in the job strip at the top.';
  } catch (error) {
    document.querySelector('#import-status').textContent = error.message;
    announce(error.message);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
});

async function startTagJob(url, payload) {
  state.busy = true;
  updateWorkflow();
  try {
    watchJob(await post(url, payload));
  } catch (error) {
    announce(error.message);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
}
document.querySelector('#tag-all').addEventListener('click', () => startTagJob('/api/photos/tag-all', { retag: document.querySelector('#retag-all').checked }));
document.querySelector('#tag-keepers').addEventListener('click', () => startTagJob('/api/photos/tag', { scope: 'keepers', ...(redoKeepers() ? { retag: true } : {}) }));
document.querySelector('#assist-keepers').addEventListener('click', () => startTagJob('/api/photos/assist', { scope: 'keepers', ...(redoKeepers() ? { redo: true } : {}) }));
// One tick for both keeper buttons: repeat tagging and scanning that is already done (and model captions for Run all).
function redoKeepers() {
  return document.querySelector('#redo-keepers').checked;
}

document.querySelector('#scan-rejects').addEventListener('click', () => startTagJob('/api/photos/review-rejects', { scope: 'keepers' }));

// Review tab: advisory reject suggestions. Rendering and polling never post; only Reject/Keep, X/K, and the counted confirm do.
const rejectList = document.querySelector('#reject-list');
const rejectConfirm = document.querySelector('#reject-all-confirm');

function problemText(problems) {
  return problems.map(item => `${item.kind.replace(/_/g, ' ')} ${Math.round(item.confidence * 100)}%`).join(', ') || 'No specific problem named';
}

function renderRejectList(suggestions) {
  state.rejectSuggestions = suggestions;
  const count = suggestions.length;
  document.querySelector('#review-count').textContent = `${count} suggested reject${count === 1 ? '' : 's'}`;
  rejectList.replaceChildren(...suggestions.map(item => {
    const row = document.createElement('li');
    row.className = 'reject-row';
    row.dataset.photoId = String(item.photo_id);
    const image = document.createElement('img');
    image.loading = 'lazy';
    image.alt = '';
    image.src = thumbUrl(item);
    const text = document.createElement('div');
    const name = document.createElement('p');
    name.textContent = item.name;
    const problems = document.createElement('p');
    problems.className = 'quiet';
    problems.textContent = problemText(item.problems);
    text.append(name, problems);
    const buttons = document.createElement('div');
    buttons.className = 'button-row';
    for (const [action, label] of [['reject', 'Reject'], ['keep', 'Keep']]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = action === 'keep' ? 'secondary' : '';
      button.dataset.rejectAction = action;
      button.textContent = label;
      button.setAttribute('aria-label', `${label} ${item.name}`);
      button.addEventListener('click', () => decideSuggestions([item.photo_id], action));
      buttons.append(button);
    }
    row.append(image, text, buttons);
    return row;
  }));
  updateWorkflow();
}

async function loadRejectSuggestions() {
  try {
    renderRejectList((await request('/api/photos/reject-suggestions')).suggestions);
  } catch (error) {
    announce(error.message);
  }
}

function rowButton(index, action) {
  const row = rejectList.children[Math.max(0, Math.min(rejectList.children.length - 1, index))];
  return row?.children[2].children[action === 'keep' ? 1 : 0] || null;
}

async function decideSuggestions(ids, decision) {
  if (state.cullingBlocked) return announce(cullingBlockedText());
  const index = state.rejectSuggestions.findIndex(item => item.photo_id === ids[0]);
  const names = new Map(state.rejectSuggestions.map(item => [item.photo_id, item.name]));
  state.busy = true;
  updateWorkflow();
  try {
    const result = await post('/api/photos/reject-suggestions/decide', { photo_ids: ids, decision });
    for (const item of result.decided) {  // the server's decision and burst pick, as after decide()
      const photo = photoById(item.photo_id);
      if (photo) recordDecision(photo, item);
    }
    const decidedIds = result.decided.map(item => item.photo_id);
    renderRejectList(state.rejectSuggestions.filter(item => !decidedIds.includes(item.photo_id)));
    const verb = decision === 'reject' ? 'rejected' : 'kept';
    announce(decidedIds.length === 1 ? `${names.get(decidedIds[0])} ${verb}` : `${decidedIds.length} frames ${verb}`);
    (rowButton(index, decision) || document.querySelector('#scan-rejects')).focus();
  } catch (error) {
    announce(error.message);
    await loadRejectSuggestions();  // a stale list (decided elsewhere) is refreshed, never applied by count
  } finally {
    state.busy = false;
    updateWorkflow();
  }
}

function handleRejectKey(event) {
  if (event.metaKey || event.ctrlKey || event.altKey || event.target?.closest('input, textarea, select')) return;
  const row = event.target?.closest('.reject-row');
  if (!row || event.target.tagName !== 'BUTTON') return;
  const key = event.key?.length === 1 ? event.key.toLowerCase() : event.key;
  const index = Array.from(rejectList.children).indexOf(row);
  if (key === 'ArrowDown' || key === 'ArrowUp') {
    event.preventDefault();
    return rowButton(index + (key === 'ArrowDown' ? 1 : -1), event.target.dataset.rejectAction)?.focus();
  }
  const decision = { x: 'reject', k: 'keep' }[key];
  if (!decision) return;
  event.preventDefault();
  return decideSuggestions([Number(row.dataset.photoId)], decision);
}
rejectList.addEventListener('keydown', handleRejectKey);

// Learned keywords: the words local models taught the vocabulary; Forget strips one from every frame and blocks it.
const vocabularyList = document.querySelector('#vocabulary-list');

async function loadVocabulary(quiet = false) {
  try {
    renderVocabulary(await request('/api/vocabulary'));
  } catch (error) {
    if (!quiet) announce(error.message);
  }
}

function renderVocabulary(vocab) {
  state.vocabularyWords = [...new Set(Object.values(vocab.categories).flat())];
  document.querySelector('#vocabulary-words').replaceChildren(...state.vocabularyWords.map(word => Object.assign(document.createElement('option'), { value: word })));
  const words = vocab.categories.learned || [];
  document.querySelector('#vocabulary-count').textContent = `(${words.length})`;
  vocabularyList.replaceChildren(...words.map((word, index) => {
    const item = document.createElement('li');
    const text = document.createElement('span');
    const frames = vocab.counts[word.toLowerCase()] || 0;
    text.textContent = `${word} (${frames} frame${frames === 1 ? '' : 's'})`;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'secondary';
    button.textContent = 'Forget';
    button.setAttribute('aria-label', `Forget keyword ${word}`);
    button.addEventListener('click', () => forgetKeyword(word, index));
    item.append(text, button);
    return item;
  }));
}

async function forgetKeyword(word, index) {
  try {
    const result = await post('/api/vocabulary/forget', { word });
    await loadVocabulary();
    announce(`${word} forgotten; removed from ${result.removed_from} frame${result.removed_from === 1 ? '' : 's'}`);
    const rows = vocabularyList.children;
    (rows[Math.min(index, rows.length - 1)]?.children[1] || document.querySelector('#vocabulary-panel summary')).focus();
    const photo = state.selectedId === null ? null : photoById(state.selectedId);
    if (photo) await inspect(photo);  // the inspector's keyword list drops the word too
  } catch (error) {
    announce(error.message);
  }
}

document.querySelector('#vocabulary-panel').addEventListener('toggle', () => {
  if (document.querySelector('#vocabulary-panel').open) loadVocabulary();
});

document.querySelector('#reject-all-shown').addEventListener('click', () => {
  const ids = state.rejectSuggestions.map(item => item.photo_id);
  if (!ids.length) return;
  rejectConfirm.dataset.ids = ids.join(',');  // the ids shown at open time, not a count
  document.querySelector('#reject-all-text').textContent = `Reject all ${ids.length} shown frames? This marks them rejected; you can restore any frame from the frame view.`;
  if (rejectConfirm.showModal) rejectConfirm.showModal();
  else rejectConfirm.hidden = false;
});
document.querySelector('#reject-all-yes').addEventListener('click', () => {
  if (rejectConfirm.close) rejectConfirm.close();
  else rejectConfirm.hidden = true;
  return decideSuggestions(rejectConfirm.dataset.ids.split(',').map(Number), 'reject');
});

const JOB_NAMES = { import: 'Import', export: 'Export', develop_group: 'Apply develop to burst', tag: 'Tagging', assist: 'Local AI assist', review_rejects: 'Reject review', triage: 'Sky triage', astro: 'Stack', ingest: 'Ingest', refine_previews: 'Refine previews' };

function jobText(job) {
  const name = JOB_NAMES[job.kind] || 'Job';
  const result = job.result || {};
  const summary = job.kind === 'import' && job.result ? ` ${result.imported} new, ${result.updated} updated, ${result.skipped} skipped, ${result.failed} failed${result.previously_cleared ? `, ${result.previously_cleared} previously cleared` : ''}${result.restored ? `, ${result.restored} restored with earlier work` : ''}.`
    : job.kind === 'tag' && job.result ? ` ${result.tagged} tagged, ${result.skipped} skipped, ${result.failed} failed.`
    : job.kind === 'assist' && job.result ? ` ${result.tagged} tagged, ${result.reviewed} scanned (${result.suggested} suggested rejects), ${result.captioned} captioned, ${result.skipped} already done, ${result.failed} failed.`
    : job.kind === 'triage' && job.result ? ` ${result.triaged} triaged, ${result.failed} failed.`
    : job.kind === 'review_rejects' && job.result ? ` ${result.reviewed} reviewed, ${result.suggested} suggested, ${result.skipped} skipped, ${result.failed} failed.`
    : job.kind === 'refine_previews' && job.result ? ` ${result.refined} refined, ${result.skipped} skipped, ${result.failed} failed.`
    : job.kind === 'export' && job.result ? ` ${result.exported} saved, ${result.failed} failed${result.skipped ? `, ${result.skipped} skipped` : ''}.${result.failed ? ' Tick Export failed in the filters to see them.' : ''}`
    : job.kind === 'develop_group' && job.result ? ` ${result.applied} developed, ${result.skipped_non_raw + result.skipped_offline} skipped, ${result.failed} failed.` : '';
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
  if (['tag', 'assist', 'review_rejects', 'triage', 'export'].includes(job.kind)) {  // AI and export jobs also report in the bottom bar
    const detail = JOB_ACTIVE.includes(job.status) && job.total ? ` ${job.processed} of ${job.total}${job.label ? ` · ${job.label}` : ''}` : '';
    aiStatus(JOB_ACTIVE.includes(job.status) ? 'running' : job.status === 'succeeded' ? 'done' : 'failed', text + detail);
  }
  if (job.kind === 'export' || job.kind === 'develop_group') renderExportDialog(job);
  if (job.kind === 'astro') setAstroJob(job);
  else {
    if (job.kind === 'ingest') renderIngest(job);
    if (job.status !== previous) announce(text);
  }
  if (JOB_ACTIVE.includes(job.status)) startJobPolling();
  else {
    stopJobPolling();
    if (JOB_ACTIVE.includes(previous)) {
      load().catch(error => announce(error.message));
      if (['triage', 'tag'].includes(job.kind) && state.selectedGroupId !== null) loadAstroTriage(state.selectedGroupId);
      if (document.querySelector('#clear-tab').getAttribute('aria-selected') === 'true') loadCleared();
    }
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

async function cancelCurrentJob() {
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
}
document.querySelector('#cancel-job').addEventListener('click', () => {
  if (JOB_ACTIVE.includes(state.job?.status)) return cancelCurrentJob();
  document.querySelector('#job-strip').hidden = true;
});

// A keepers export runs as a job; this modal shows its progress and offers Cancel, then the outcome and Close.
const exportDialog = document.querySelector('#export-dialog');
// Shared by the export job and the burst develop job: one progress dialog, named after the job it watches.
function renderExportDialog(job) {
  const active = JOB_ACTIVE.includes(job.status);
  document.querySelector('#export-dialog-heading').textContent = job.kind === 'export' ? 'Exporting JPEGs' : JOB_NAMES[job.kind];
  const percent = Math.round((job.progress || 0) * 100);
  const progress = document.querySelector('#export-dialog-progress');
  progress.value = percent;
  progress.setAttribute('aria-valuetext', `${percent}% complete`);
  document.querySelector('#export-dialog-label').textContent = job.total ? `${job.processed} of ${job.total}${job.label ? ` · ${job.label}` : ''}` : 'Starting…';
  const text = jobText(job);
  const status = document.querySelector('#export-dialog-status');
  if (status.textContent !== text) status.textContent = text;
  document.querySelector('#export-dialog-cancel').hidden = !active;
  document.querySelector('#export-dialog-close').hidden = active;
  if (active && !exportDialog.open) { if (exportDialog.showModal) exportDialog.showModal(); else exportDialog.hidden = false; }
  if (!active && job.kind === 'export') {
    document.querySelector('#export-status').textContent = text;
    const list = document.querySelector('#export-results');
    list.replaceChildren();
    (job.log_tail || []).forEach(line => { const item = document.createElement('li'); item.textContent = line; list.append(item); });
  }
}
document.querySelector('#export-dialog-cancel').addEventListener('click', cancelCurrentJob);
document.querySelector('#export-dialog-close').addEventListener('click', () => { if (exportDialog.close) exportDialog.close(); else exportDialog.hidden = true; });

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
    const triage = document.createElement('span');
    triage.className = 'triage quiet';
    triage.textContent = 'not triaged';
    label.append(input, name, triage);
    list.append(label);
  });
  if (group) loadAstroTriage(group.id);
}

// Astro triage is advice: it only writes text; checkboxes change only through the counted confirm below.
function triageText(light) {
  if (!light) return 'not triaged';
  const parts = [light.sky ? `${light.sky.class.replace(/_/g, ' ')} ${Math.round(light.sky.confidence * 100)}%` : 'not triaged',
    ...light.issues.map(issue => issue.replace(/_/g, ' '))];
  if (light.trailing_warning && !light.issues.includes('trailing')) parts.push('trailing measured');
  if (light.flagged) parts.push('flagged');
  return parts.join(' · ');
}

async function loadAstroTriage(groupId) {
  try {
    const triage = await request(`/api/groups/${groupId}/triage`);
    if (groupId !== state.selectedGroupId) return;
    state.astro.triage = triage;
  } catch (error) {
    return announce(error.message);
  }
  const lights = new Map(state.astro.triage.lights.map(light => [light.photo_id, light]));
  Array.from(document.querySelector('#astro-lights').children).forEach(label => {
    label.children[2].textContent = triageText(lights.get(Number(label.children[0].dataset.id)));
  });
  updateWorkflow();
}

function flaggedLights() {
  const flagged = new Set((state.astro.triage?.lights || []).filter(light => light.flagged).map(light => String(light.photo_id)));
  return Array.from(document.querySelectorAll('#astro-lights input')).filter(input => input.checked && flagged.has(String(input.dataset.id)));
}

function astroLights() {
  const inputs = Array.from(document.querySelectorAll('#astro-lights input'));
  return { included: inputs.filter(input => input.checked).length, excluded: inputs.filter(input => !input.checked).map(input => Number(input.dataset.id)) };
}

const excludeConfirm = document.querySelector('#exclude-confirm');
const lightsText = count => `${count} light${count === 1 ? '' : 's'}`;
document.querySelector('#triage-astro').addEventListener('click', () => {
  if (state.selectedGroupId !== null) return startTagJob(`/api/groups/${state.selectedGroupId}/triage`, {});
});
document.querySelector('#exclude-flagged').addEventListener('click', () => {
  const ids = flaggedLights().map(input => String(input.dataset.id));
  if (!ids.length) return;
  excludeConfirm.dataset.ids = ids.join(',');  // the lights flagged at open time, not a count
  document.querySelector('#exclude-confirm-text').textContent = `Uncheck ${ids.length} flagged light${ids.length === 1 ? '' : 's'}? Flagged means cloud, not sky, trailing, or a cloud band. Nothing is stacked until you prepare and run.`;
  if (excludeConfirm.showModal) excludeConfirm.showModal();
  else excludeConfirm.hidden = false;
});
document.querySelector('#exclude-confirm-yes').addEventListener('click', () => {
  if (excludeConfirm.close) excludeConfirm.close();
  else excludeConfirm.hidden = true;
  const ids = new Set(excludeConfirm.dataset.ids.split(','));
  const unchecked = Array.from(document.querySelectorAll('#astro-lights input')).filter(input => input.checked && !input.disabled && ids.has(String(input.dataset.id)));
  unchecked.forEach(input => { input.checked = false; });  // client state only; prepare sends it as excluded_photo_ids
  updateWorkflow();
  announce(`${lightsText(unchecked.length)} excluded`);
});

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
  document.querySelector('#astro-summary').textContent = `Job ${job.job_id} · ${job.lights} lights, ${job.excluded_lights} excluded · ${job.darks} darks · ${job.flats} flats · ${job.biases} biases${job.recipe ? ` · Recipe: ${job.recipe}` : ''}`;
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
  const recipe = document.querySelector('input[name="astro-recipe"]:checked').value;
  setAstroJob(await post('/api/astro/jobs', { group_id: state.selectedGroupId, confirmed: true, excluded_photo_ids: excluded, recipe, ...state.astro.folders }));
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
  const keepers = state.keeperCount;  // library-wide, like export; never the filtered view
  const available = state.status?.tools?.['darktable-cli']?.available === true;
  document.querySelector('#current-export-name').textContent = photo?.name || 'No photo selected';
  document.querySelector('#keeper-count').textContent = `${keepers} keeper${keepers === 1 ? '' : 's'}`;
  const filtered = pagedPhotos().length;  // every frame the filters match, not only the page shown
  document.querySelector('#filtered-export-count').textContent = location.search ? frameCount(filtered) : `${frameCount(filtered)}, no filter on`;
  document.querySelector('#rejected-count').textContent = `(${state.rejectedCount})`;
  document.querySelectorAll('[data-decision-control]').forEach(button => { button.disabled = state.cullingBlocked; });
  const rating = document.querySelector('#rating');
  if (rating) rating.disabled = state.cullingBlocked;
  document.querySelector('#group-cleanup-count').textContent = group ? `Burst ${groupIndex + 1} · ${frameCount(group.frame_count)}` : 'No batch selected · 0 frames';
  document.querySelector('#workspace-cleanup-count').textContent = `${state.groups.length} batch${state.groups.length === 1 ? '' : 'es'} · ${frameCount(state.photoCount)}`;
  document.querySelector('#clear-help').textContent = `Use the exact uppercase word CLEAR. This removes all ${frameCount(state.photoCount)} from Lumen, including photos outside visible batches.`;
  document.querySelector('#export-availability').textContent = available ? 'darktable is ready. Existing JPEGs are kept; each export gets a unique name.' : 'darktable-cli is unavailable. Install darktable or add it to PATH, restart Lumen, then reload this page.';
  const scope = document.querySelector('input[name="export-scope"]:checked').value;
  document.querySelector('#export-jpegs').disabled = state.busy || !available || (scope === 'current' ? !photo : scope === 'keepers' ? !keepers : !filtered);
  document.querySelector('#loupe-export').disabled = state.busy || !available || !photo;
  document.querySelector('#refine-availability').textContent = available ? '' : 'darktable-cli is unavailable. Install darktable or add it to PATH, restart Lumen, then reload this page.';
  document.querySelector('#refine-previews').disabled = state.busy || !available || (document.querySelector('#refine-scope').value === 'group' && !group);
  document.querySelector('#remove-group').disabled = state.busy || !group;
  document.querySelector('#remove-cleared').disabled = state.busy || !state.clearedFrames;
  document.querySelector('#merge-batches').disabled = state.busy || state.mergeSelection.size < 2;
  document.querySelector('#merge-batches').hidden = document.querySelector('#merge-count').hidden = state.mergeSelection.size === 0;
  document.querySelector('#jump-selected').hidden = state.selectedGroupId === null;
  document.querySelector('#write-digest').disabled = state.busy;
  document.querySelector('#merge-count').textContent = `${state.mergeSelection.size} batch${state.mergeSelection.size === 1 ? '' : 'es'} selected`;
  document.querySelectorAll('[data-merge-group]').forEach(box => { box.checked = state.mergeSelection.has(Number(box.dataset.mergeGroup)); });
  document.querySelectorAll('[data-burst]').forEach(section => {
    const selected = Number(section.dataset.burst) === state.selectedGroupId;
    section.dataset.selected = String(selected);
    section.children[0].children[2].textContent = selected ? 'Selected' : '';
  });
  document.querySelector('#clear-workspace').disabled = state.busy || !state.photoCount || document.querySelector('#clear-confirmation').value !== 'CLEAR';
  document.querySelector('#browse-xmp').disabled = state.busy;
  document.querySelector('#reset-xmp').disabled = state.busy || !state.profile;
  document.querySelector('#import-form button[type="submit"]').disabled = state.busy;
  document.querySelector('#browse-path').disabled = state.busy;
  document.querySelector('#open-compare').disabled = state.cullingBlocked || state.compare.length !== 2;
  document.querySelector('#open-compare').hidden = state.compare.length === 0;
  const fastLoaded = roleReady('fast');
  // The not-loaded sentence is said once in the footer, and again atop the frame dialog, which hides the footer.
  const unavailable = roleUnavailable('fast') || roleUnavailable('review') || '';
  document.querySelector('#ai-availability').textContent = unavailable;
  document.querySelector('#frame-ai-availability').textContent = unavailable;
  document.querySelector('#tag-all').disabled = state.busy || !fastLoaded;
  document.querySelector('#tag-keepers').textContent = `Tag keepers (${keepers})`;
  document.querySelector('#tag-keepers').disabled = state.busy || !keepers || !fastLoaded;
  updateExpandSearch();
  const bothLoaded = fastLoaded && roleReady('review');
  document.querySelector('#assist-keepers').textContent = `Tag, scan and caption keepers (${keepers})`;
  document.querySelector('#assist-keepers').disabled = state.busy || !keepers || !bothLoaded;
  document.querySelector('#scan-rejects').disabled = state.busy || !keepers || !fastLoaded;
  document.querySelector('#reject-all-shown').disabled = state.cullingBlocked || !state.rejectSuggestions.length;
  document.querySelector('#retag-all').disabled = state.busy;
  document.querySelector('#redo-keepers').disabled = state.busy;
  document.querySelector('#ask-composition-ai').disabled = state.busy || !roleReady('review') || !state.composition || state.composition.photo_id !== state.selectedId || aiRunning(`composition:${state.selectedId}`);
  document.querySelectorAll('[data-crop-action]').forEach(button => { button.disabled = state.busy || button.closest?.('.crop-card')?.classList?.contains('active'); });
  document.querySelectorAll('.batch-button').forEach((button, index) => button.setAttribute('aria-pressed', String(state.groups[index].id === state.selectedGroupId)));

  syncDevelopControls();
  renderAstroLights(group);
  const siril = state.status?.tools?.['siril-cli']?.available === true;
  const job = state.astro.job;
  const activeJob = JOB_ACTIVE.includes(job?.status);
  document.querySelector('#astro-group-count').textContent = group ? `Burst ${groupIndex + 1} · ${frameCount(group.photos.length)}` : 'No batch selected';
  document.querySelector('#astro-availability').textContent = siril ? 'Siril is ready. One stack runs at a time; everything is written below the library outputs folder.' : 'siril-cli is unavailable. Install Siril or add it to PATH, restart Lumen, then reload this page.';
  document.querySelector('#prepare-astro').disabled = state.busy || !siril || !group || !document.querySelector('#astro-confirm').checked || astroLights().included < 2 || activeJob;
  document.querySelector('#run-astro').disabled = state.busy || job?.status !== 'queued';
  document.querySelectorAll('#astro-lights input').forEach(input => { input.disabled = activeJob; });
  const flagged = flaggedLights().length;
  document.querySelector('#triage-astro').disabled = state.busy || !group || !roleReady('fast');
  document.querySelector('#exclude-flagged').textContent = `Exclude flagged lights (${flagged})`;
  document.querySelector('#exclude-flagged').disabled = activeJob || !flagged;
  document.querySelector('#astro-confirm').disabled = activeJob;
  document.querySelectorAll('input[name="astro-recipe"]').forEach(input => { input.disabled = activeJob; });
  document.querySelector('#cancel-astro').disabled = state.acting || !activeJob;
  // One button: Cancel while the job runs, Clear (hides the strip) once it has ended.
  const jobButton = document.querySelector('#cancel-job');
  const jobActive = JOB_ACTIVE.includes(state.job?.status);
  jobButton.disabled = state.acting || !state.job;
  jobButton.textContent = jobActive ? 'Cancel job' : 'Clear';
  jobButton.className = jobActive ? 'danger' : 'secondary';
  document.querySelector('#reveal-astro').disabled = state.busy || !job;
  for (const kind of ['card', 'primary', 'mirror']) document.querySelector(`#browse-ingest-${kind}`).disabled = state.busy;
  document.querySelector('#reset-ingest-mirror').disabled = state.busy || !state.ingest.mirror;
  document.querySelector('#start-ingest').disabled = state.busy || !state.ingest.card || !state.ingest.primary;
  for (const kind of ['darks', 'flats', 'biases']) {
    document.querySelector(`#browse-astro-${kind}`).disabled = state.busy || activeJob;
    document.querySelector(`#reset-astro-${kind}`).disabled = state.busy || activeJob || !state.astro.folders[kind];
  }
}

// Map tab: Web Mercator over the offline base map, frames as dots or clusters, overlays and geotagging beside it.
const mapCanvas = document.querySelector('#map-canvas');
const mapStatus = document.querySelector('#map-status');
const MAP_MAX_LAT = 85.0511;
const MAP_ZOOM = { min: 1, max: 18 };
const mapShowAll = document.querySelector('#map-show-all');
const mapCard = document.querySelector('#map-card');
const MAP_THUMB_ZOOM = 9;  // lone frames draw as thumbs from here in
const MAP_FAN_SIZE = 12;
// Best first wherever a cluster's frames are shown: rejects last, then flagged, rating desc, time.
const byFanOrder = (a, b) => (a.decision === 'reject') - (b.decision === 'reject')
  || (b.decision === 'flag') - (a.decision === 'flag') || (b.rating || 0) - (a.rating || 0)
  || (a.captured_at || '~').localeCompare(b.captured_at || '~');
const mapImages = new Map();  // photo id (or cleared key) -> Image, so a thumb loads once
// Cleared frames from the trash sit in photos too, with `cleared: true` and `key` in place of `id`.
state.map = { zoom: 2, center: { lat: 20, lon: 0 }, photos: [], features: [], overlays: [], selected: null, scope: 'shown',
  unfixed: 0, focus: null, fitPending: false, hits: [], framesKey: '', drag: null, featureRequest: 0, building: false, fan: null };
const mapKey = photo => (photo.cleared ? `cleared-${photo.key}` : photo.id);

const worldSize = zoom => 256 * 2 ** zoom;
function project(lat, lon, zoom) {
  const size = worldSize(zoom);
  const sin = Math.sin(Math.max(-MAP_MAX_LAT, Math.min(MAP_MAX_LAT, lat)) * Math.PI / 180);
  return { x: (lon + 180) / 360 * size, y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * size };
}
function unproject(x, y, zoom) {
  const size = worldSize(zoom);
  const lon = ((x / size * 360) % 360 + 360) % 360 - 180;  // wrapped into -180..180
  const lat = Math.atan(Math.sinh(Math.PI * (1 - 2 * y / size))) * 180 / Math.PI;
  return { lat, lon };
}
function mapSize() {
  return { width: mapCanvas.clientWidth, height: mapCanvas.clientHeight };
}
const clampZoom = zoom => Math.max(MAP_ZOOM.min, Math.min(MAP_ZOOM.max, zoom));

// ponytail: a set of points straddling 180 degrees fits the long way round; unwrap the longitudes if that ever matters.
function fitBounds(points, padding = 0.1) {
  const { width, height } = mapSize();
  if (!width || !height) return false;
  if (!points.length) {
    Object.assign(state.map, { center: { lat: 20, lon: 0 }, zoom: clampZoom(Math.log2(width / 256)) });
    return true;
  }
  const lats = points.map(point => point.lat);
  const lons = points.map(point => point.lon);
  const corner = project(Math.max(...lats), Math.min(...lons), 0);
  const opposite = project(Math.min(...lats), Math.max(...lons), 0);
  const span = { x: opposite.x - corner.x, y: opposite.y - corner.y };
  const usable = 1 - 2 * padding;
  const zoom = span.x || span.y
    ? Math.log2(Math.min(span.x ? width * usable / span.x : Infinity, span.y ? height * usable / span.y : Infinity))
    : 12;
  state.map.zoom = clampZoom(Math.min(zoom, 15));
  state.map.center = unproject((corner.x + opposite.x) / 2, (corner.y + opposite.y) / 2, 0);
  return true;
}

// A world x is drawn at whichever copy of the world lies nearest `near`, so the view crosses 180 degrees cleanly.
function toScreen(lat, lon, near = null) {
  const { width, height } = mapSize();
  const size = worldSize(state.map.zoom);
  const centre = project(state.map.center.lat, state.map.center.lon, state.map.zoom);
  const point = project(lat, lon, state.map.zoom);
  let x = point.x - centre.x + width / 2;
  const target = near ?? width / 2;
  x += Math.round((target - x) / size) * size;
  return { x, y: point.y - centre.y + height / 2 };
}
function viewBox() {
  const { width, height } = mapSize();
  const { zoom } = state.map;
  const centre = project(state.map.center.lat, state.map.center.lon, zoom);
  const north = unproject(centre.x, Math.max(0, centre.y - height / 2), zoom).lat;
  const south = unproject(centre.x, Math.min(worldSize(zoom), centre.y + height / 2), zoom).lat;
  if (width >= worldSize(zoom)) return { south, west: -180, north, east: 180 };
  return { south, north, west: unproject(centre.x - width / 2, 0, zoom).lon, east: unproject(centre.x + width / 2, 0, zoom).lon };
}
const kmPerPixel = () => 360 / worldSize(state.map.zoom) * Math.cos(state.map.center.lat * Math.PI / 180) * 111.32;

// refit false keeps the centre and zoom, as after a geotag or a filter change made from the map.
async function loadMap(refit = true) {
  const scope = document.querySelector('input[name="map-scope"]:checked')?.value || 'shown';
  state.map.scope = scope;
  foldMap();
  try {
    const [photos, overlays] = await Promise.all([
      request(`/api/map/photos${{ shown: location.search, ever: '?include_cleared=1&show_rejected=1' }[scope] || ''}`),
      request('/api/map/overlays'),
    ]);
    state.map.photos = [...photos.photos, ...photos.cleared.map(frame => ({ ...frame, cleared: true }))];
    state.map.unfixed = photos.unfixed;
    state.map.overlays = overlays.overlays;
  } catch (error) {
    mapStatus.textContent = error.message;
    state.map.focus = null;
    return;
  }
  if (!state.map.building) mapStatus.textContent = '';
  renderOverlays();
  renderGeotag();
  if (!refit) {
    drawMap();
    return loadFeatures();
  }
  state.map.fitPending = true;
  await fitMap();
}

// The frame asked for, else the frames, else the overlays' tracks and waypoints, else the world.
async function fitMap() {
  if (!mapSize().width) return;  // the panel is folded; the resize that unfolds it fits
  state.map.fitPending = false;
  foldMap();
  const { focus } = state.map;
  if (focus) {
    Object.assign(state.map, { center: focus, zoom: 12, focus: null });
  } else if (state.map.photos.length) {
    fitBounds(state.map.photos.map(photo => ({ lat: photo.latitude, lon: photo.longitude })));
  } else {
    let points = [];
    if (state.map.overlays.some(overlay => overlay.tracks || overlay.waypoints)) {
      try {
        const world = await request('/api/map/features?bbox=-85,-180,85,180&zoom=1');
        points = (world.features || []).filter(feature => ['track', 'waypoint'].includes(feature.kind))
          .flatMap(feature => geometryPoints(feature.geometry));
      } catch { /* the world view below still draws */ }
    }
    fitBounds(points);
  }
  drawMap();
  loadFeatures();
}
function geometryPoints(geometry) {
  const flat = coordinates => (typeof coordinates[0] === 'number' ? [{ lon: coordinates[0], lat: coordinates[1] }] : coordinates.flatMap(flat));
  return flat(geometry.coordinates);
}

let featureTimer = null;
function scheduleFeatures() {
  window.clearTimeout(featureTimer);
  featureTimer = window.setTimeout(loadFeatures, 150);
}
// A newer viewport wins; an index still building answers {building: true} and is asked again in two seconds.
async function loadFeatures() {
  window.clearTimeout(featureTimer);
  if (!mapSize().width) return;
  const requestId = ++state.map.featureRequest;
  const box = viewBox();
  const bbox = [box.south, box.west, box.north, box.east].map(value => value.toFixed(5)).join(',');
  let answer;
  try {
    answer = await request(`/api/map/features?bbox=${bbox}&zoom=${state.map.zoom.toFixed(2)}`);
  } catch (error) {
    if (requestId === state.map.featureRequest) mapStatus.textContent = error.message;
    return;
  }
  if (requestId !== state.map.featureRequest) return;
  if (answer.building) {
    state.map.building = true;
    mapStatus.textContent = 'Building map index…';
    featureTimer = window.setTimeout(loadFeatures, 2000);
    return;
  }
  if (state.map.building) mapStatus.textContent = '';
  state.map.building = false;
  state.map.features = answer.features;
  drawMap();
}

const MAP_ORDER = ['land', 'mountain', 'plain', 'desert', 'lake', 'coastline', 'border', 'river', 'road', 'park', 'sea', 'region', 'peak',
  'track', 'waypoint', 'place'];
const MAP_FONT = '12px "Avenir Next", Avenir, system-ui, sans-serif';
// The two natural palettes; Workbench reads the page's own tokens. Frames, clusters, tracks and the selection keep amber/cyan/charcoal.
const MAP_PALETTES = {
  'natural-dark': { sea: '#142a40', land: '#29261f', water: '#21507a', waterline: '#5b9ccc', mountain: '#9a6438', plain: '#4f9440',
    desert: '#d2ad68', label: '#a9b2bc', sealabel: '#8fbbe0', line: '#544f46', muted: '#a9b2bc' },
  'natural-light': { sea: '#b8d5ea', land: '#f2ebd8', water: '#93c3e4', waterline: '#3f84bb', mountain: '#a8703f', plain: '#7fb85a',
    desert: '#ddbb73', label: '#3a362f', sealabel: '#336f9f', line: '#9c9384', muted: '#5c564c' },
};
const mapStyleSelect = document.querySelector('#map-style');
function mapStyle() {
  let stored = null;
  try { stored = localStorage.getItem('lumen-map-style'); } catch { /* per-visit only */ }
  if (stored && [...mapStyleSelect.options].some(option => option.value === stored)) mapStyleSelect.value = stored;
  return mapStyleSelect.value;
}
mapStyle();
let mapColours = null;
function mapColour(name) {
  if (!mapColours) {
    const style = window.getComputedStyle(document.documentElement);
    const token = Object.fromEntries(['charcoal', 'graphite', 'line', 'muted', 'amber', 'cyan'].map(key => [key, style.getPropertyValue(`--${key}`).trim()]));
    const chosen = mapStyleSelect.value;
    const base = MAP_PALETTES[chosen] || { sea: token.charcoal, land: token.graphite, water: token.charcoal, waterline: token.cyan,
      mountain: null, plain: null, desert: null, label: token.muted, sealabel: token.muted, line: token.line, muted: token.muted };
    mapColours = { ...token, ...base, tint: chosen === 'natural-light' ? 0.28 : 0.18 };
  }
  return mapColours[name];
}
mapStyleSelect.addEventListener('change', () => {
  try { localStorage.setItem('lumen-map-style', mapStyleSelect.value); } catch { /* per-visit only */ }
  mapColours = null;
  drawMap();
});

// Each ring or line as one subpath; consecutive vertices unwrap against each other so a line over 180 degrees stays whole.
// Returns the screen bounding box of what it traced.
function traceGeometry(context, geometry) {
  const lines = { LineString: [geometry.coordinates], MultiLineString: geometry.coordinates, Polygon: geometry.coordinates,
    MultiPolygon: (geometry.coordinates || []).flat() }[geometry.type] || [];
  const box = { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity };
  context.beginPath();
  for (const line of lines) {
    let previous = null;
    line.forEach(([lon, lat], index) => {
      const point = toScreen(lat, lon, previous?.x);
      if (index) context.lineTo(point.x, point.y);
      else context.moveTo(point.x, point.y);
      previous = point;
      box.left = Math.min(box.left, point.x);
      box.right = Math.max(box.right, point.x);
      box.top = Math.min(box.top, point.y);
      box.bottom = Math.max(box.bottom, point.y);
    });
    if (geometry.type.endsWith('Polygon')) context.closePath();
  }
  return box;
}

const MAP_NAMED_AREAS = new Set(['sea', 'mountain', 'plain', 'desert', 'region']);
// A sea or terrain name, only once its scalerank is within zoom - 1; sea names pale-blue italic.
function drawAreaLabel(context, feature, x, y) {
  if (!feature.name || !(feature.rank <= state.map.zoom - 1)) return;
  context.globalAlpha = 1;
  context.fillStyle = mapColour(feature.kind === 'sea' ? 'sealabel' : 'label');
  if (feature.kind === 'sea') context.font = `italic ${MAP_FONT}`;
  context.textAlign = 'center';
  context.fillText(feature.name, x, y);
  context.textAlign = 'start';
  context.font = MAP_FONT;
}

function drawFeature(context, feature) {
  const { kind, geometry } = feature;
  const zoom = state.map.zoom;
  context.setLineDash([]);
  context.globalAlpha = 1;
  if (geometry.type === 'Point') {
    const [lon, lat] = geometry.coordinates;
    const point = toScreen(lat, lon);
    if (kind === 'waypoint') {
      context.fillStyle = mapColour('amber');
      context.beginPath();
      context.arc(point.x, point.y - 10, 5, Math.PI, 0);
      context.lineTo(point.x, point.y);
      context.closePath();
      context.fill();
      if (zoom >= 8 && feature.name) context.fillText(feature.name, point.x + 8, point.y - 8);
    } else if (kind === 'peak') {
      context.fillStyle = mapColour('label');
      context.beginPath();
      context.moveTo(point.x, point.y - 5);
      context.lineTo(point.x + 5, point.y + 4);
      context.lineTo(point.x - 5, point.y + 4);
      context.closePath();
      context.fill();
      if (zoom >= 8 && feature.name) context.fillText(feature.name, point.x + 8, point.y);
    } else if (MAP_NAMED_AREAS.has(kind)) {
      drawAreaLabel(context, feature, point.x, point.y);
    } else if (kind === 'place' && (zoom >= 7 || (zoom >= 5 && feature.rank <= 3)) && feature.name) {
      context.fillStyle = mapColour('muted');
      context.fillText(feature.name, point.x + 4, point.y);
    }
    return;
  }
  if (kind === 'land') {
    traceGeometry(context, geometry);
    context.fillStyle = mapColour('land');
    context.fill('evenodd');
    return;
  }
  if (MAP_NAMED_AREAS.has(kind)) {  // sea and region are named only; mountain, plain and desert tinted when the style has a tint
    const box = traceGeometry(context, geometry);
    const tint = kind === 'sea' || kind === 'region' ? null : mapColour(kind);
    if (tint) {
      context.fillStyle = tint;
      context.globalAlpha = mapColour('tint');
      context.fill('evenodd');
    }
    if (Math.max(box.right - box.left, box.bottom - box.top) >= 80) drawAreaLabel(context, feature, (box.left + box.right) / 2, (box.top + box.bottom) / 2);
    return;
  }
  const styles = {
    lake: ['waterline', 1], coastline: ['line', 1], border: ['line', 1, [6, 4]], river: ['waterline', 1], road: ['muted', 0.75],
    park: ['amber', 1, [4, 3]], track: ['amber', 2],
  };
  const [colour, width, dash] = styles[kind] || ['line', 1];
  traceGeometry(context, geometry);
  if (kind === 'lake') {
    context.fillStyle = mapColour('water');
    context.fill('evenodd');
    context.globalAlpha = 0.4;
  }
  context.strokeStyle = mapColour(colour);
  context.lineWidth = width;
  if (dash) context.setLineDash(dash);
  context.stroke();
}

// ponytail: greedy O(frames x clusters); a grid bucket if thousands of frames ever share one view.
function clusterPhotos() {
  const { width, height } = mapSize();
  const clusters = [];
  for (const photo of state.map.photos) {
    const point = toScreen(photo.latitude, photo.longitude);
    if (point.x < -24 || point.y < -24 || point.x > width + 24 || point.y > height + 24) continue;
    const near = clusters.find(cluster => Math.hypot(cluster.x - point.x, cluster.y - point.y) < 24);
    if (near) near.photos.push(photo);
    else clusters.push({ ...point, photos: [photo] });
  }
  return clusters;
}

function thumbFor(photo) {
  if (!photo.thumb_url) return null;  // a cleared frame whose preview did not reach the trash
  let image = mapImages.get(mapKey(photo));
  if (!image) {
    image = new Image();
    image.addEventListener('load', () => drawMap());
    image.src = photo.thumb_url;
    mapImages.set(mapKey(photo), image);
  }
  return image.complete && image.naturalWidth ? image : null;
}

let mapFrame = 0;
function drawMap() {
  window.cancelAnimationFrame(mapFrame);
  mapFrame = window.requestAnimationFrame(paintMap);
}
function paintMap() {
  const { width, height } = mapSize();
  if (!width || !height) return;
  const ratio = window.devicePixelRatio || 1;
  if (mapCanvas.width !== Math.round(width * ratio) || mapCanvas.height !== Math.round(height * ratio)) {
    mapCanvas.width = Math.round(width * ratio);
    mapCanvas.height = Math.round(height * ratio);
  }
  const context = mapCanvas.getContext('2d');
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.fillStyle = mapColour('sea');
  context.fillRect(0, 0, width, height);
  context.font = MAP_FONT;
  context.textBaseline = 'middle';
  const rank = kind => (MAP_ORDER.includes(kind) ? MAP_ORDER.indexOf(kind) : 0);
  [...state.map.features].sort((a, b) => rank(a.kind) - rank(b.kind)).forEach(feature => drawFeature(context, feature));
  context.setLineDash([]);
  context.globalAlpha = 1;
  const clusters = clusterPhotos();
  state.map.hits = [];
  for (const cluster of clusters) {
    if (cluster.photos.length > 1) {
      const radius = Math.min(22, 10 + 3 * Math.log2(cluster.photos.length));
      const colour = cluster.photos.some(photo => photo.id === state.map.selected) ? mapColour('amber') : mapColour('cyan');
      // From thumb zoom a cluster shows its best frame with the count as a corner badge; the disc until it loads.
      const best = state.map.zoom >= MAP_THUMB_ZOOM ? [...cluster.photos].sort(byFanOrder)[0] : null;
      const image = best ? thumbFor(best) : null;
      let count = { x: cluster.x, y: cluster.y };
      let hitRadius = radius;
      if (image) {
        const scale = 48 / Math.max(image.naturalWidth, image.naturalHeight);
        const [w, h] = [image.naturalWidth * scale, image.naturalHeight * scale];
        context.drawImage(image, cluster.x - w / 2, cluster.y - h / 2, w, h);
        context.strokeStyle = colour;
        context.lineWidth = 2;
        if (best.cleared) context.setLineDash([4, 3]);
        context.strokeRect(cluster.x - w / 2, cluster.y - h / 2, w, h);
        outlineMarker(context, () => context.rect(cluster.x - w / 2 - 1.75, cluster.y - h / 2 - 1.75, w + 3.5, h + 3.5));
        count = { x: cluster.x + w / 2, y: cluster.y - h / 2 };
        hitRadius = Math.hypot(w, h) / 2;  // the whole thumb answers a click
        context.fillStyle = mapColour('amber');
        context.beginPath();
        context.arc(count.x, count.y, Math.max(9, context.measureText(String(cluster.photos.length)).width / 2 + 4), 0, 2 * Math.PI);
        context.fill();
        outlineMarker(context);
      } else {
        context.fillStyle = colour;
        context.beginPath();
        context.arc(cluster.x, cluster.y, radius, 0, 2 * Math.PI);
        context.fill();
        outlineMarker(context);
      }
      context.fillStyle = mapColour('charcoal');
      context.textAlign = 'center';
      context.fillText(String(cluster.photos.length), count.x, count.y);
      context.textAlign = 'start';
      state.map.hits.push({ ...cluster, radius: hitRadius });
      continue;
    }
    const [photo] = cluster.photos;
    state.map.hits.push(drawFrame(context, photo, cluster, state.map.zoom >= MAP_THUMB_ZOOM));
  }
  const ring = paintFan(context);
  drawScale();
  const centre = state.map.center;
  const visible = clusters.reduce((sum, cluster) => sum + cluster.photos.length, 0);
  const tracks = state.map.features.filter(feature => feature.kind === 'track').length;
  mapCanvas.setAttribute('aria-label', `Map centred on ${centre.lat.toFixed(3)}, ${centre.lon.toFixed(3)}, ${roundKm(kmPerPixel() * width)} km across, ${visible} frames in view, ${tracks} tracks`);
  const singles = clusters.filter(cluster => cluster.photos.length === 1).map(cluster => cluster.photos[0]);
  renderFramesInView([...singles, ...ring.filter(photo => !singles.includes(photo))]);
}

// A 1.5 px charcoal outline round the current path (or the one `trace` adds), so the cyan and amber markers keep
// their colours and still read on a light style's land.
function outlineMarker(context, trace) {
  if (trace) {
    context.beginPath();
    trace();
  }
  context.setLineDash([]);
  context.strokeStyle = mapColour('charcoal');
  context.lineWidth = 1.5;
  context.stroke();
}

// One frame at a screen point: its thumb when asked for and loaded, else a dot (hollow for a cleared frame). Returns its hit.
function drawFrame(context, photo, at, withThumb) {
  const colour = photo.id === state.map.selected ? mapColour('amber') : mapColour('cyan');
  const image = withThumb ? thumbFor(photo) : null;
  context.lineWidth = 2;
  if (image) {
    const scale = 48 / Math.max(image.naturalWidth, image.naturalHeight);
    const [w, h] = [image.naturalWidth * scale, image.naturalHeight * scale];
    context.drawImage(image, at.x - w / 2, at.y - h / 2, w, h);
    context.strokeStyle = colour;
    if (photo.cleared) context.setLineDash([4, 3]);  // a cleared thumb is framed dashed, as its dot is hollow
    context.strokeRect(at.x - w / 2, at.y - h / 2, w, h);
    outlineMarker(context, () => context.rect(at.x - w / 2 - 1.75, at.y - h / 2 - 1.75, w + 3.5, h + 3.5));
    return { x: at.x, y: at.y, photos: [photo], radius: 24 };
  }
  context.beginPath();
  context.arc(at.x, at.y, 6, 0, 2 * Math.PI);
  if (photo.cleared) {  // the charcoal ring sits just outside the 2 px coloured one
    context.strokeStyle = colour;
    context.stroke();
    outlineMarker(context, () => context.arc(at.x, at.y, 7.75, 0, 2 * Math.PI));
  } else {
    context.fillStyle = colour;
    context.fill();
    outlineMarker(context);
  }
  return { x: at.x, y: at.y, photos: [photo], radius: 10 };
}

// The open fan: up to MAP_FAN_SIZE frames on a ring round the cluster's point, their hits ahead of the cluster's,
// and the Show all button under the ring. Returns the frames drawn on the ring.
function paintFan(context) {
  const { fan } = state.map;
  if (!fan) {
    mapShowAll.hidden = true;
    return [];
  }
  const centre = toScreen(fan.at.lat, fan.at.lon);
  const shown = fan.members.slice(0, MAP_FAN_SIZE);
  const radius = Math.max(56, 8 * shown.length);  // 96 px at 12: slots about 50 px apart, so 48 px thumbs do not overlap
  const hits = shown.map((photo, index) => {
    const angle = -Math.PI / 2 + 2 * Math.PI * index / shown.length;
    const slot = { x: centre.x + radius * Math.cos(angle), y: centre.y + radius * Math.sin(angle) };
    context.strokeStyle = mapColour('line');
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(centre.x, centre.y);
    context.lineTo(slot.x, slot.y);
    context.stroke();
    return drawFrame(context, photo, slot, true);
  });
  state.map.hits.unshift(...hits);
  const live = fan.members.filter(photo => !photo.cleared);
  const cleared = fan.members.length - live.length;
  mapShowAll.hidden = !live.length;
  if (live.length) {
    const ids = live.slice(0, 200);  // the ids filter's ceiling
    mapShowAll.textContent = `Show ${ids.length < live.length ? `the first 200 of ${live.length}` : `all ${live.length}`} in Review${cleared ? ` (${cleared} cleared)` : ''}`;
    const { width, height } = mapSize();
    const half = mapShowAll.offsetWidth / 2;
    mapShowAll.style.left = `${Math.max(half + 8, Math.min(width - half - 8, centre.x))}px`;
    mapShowAll.style.top = `${Math.max(8, Math.min(height - mapShowAll.offsetHeight - 8, centre.y + radius + 32))}px`;
  }
  return shown;
}

const MAP_DECISIONS = { flag: 'flagged', reject: 'rejected' };
function fanOut(hit) {
  const [anchor] = hit.photos;  // a cluster sits at its first frame's point
  state.map.fan = { at: { lat: anchor.latitude, lon: anchor.longitude }, members: [...hit.photos].sort(byFanOrder) };
  drawMap();
  const live = hit.photos.filter(photo => !photo.cleared).length;
  announce(`${plural(hit.photos.length, 'frame')} fanned out${live < hit.photos.length ? `, ${hit.photos.length - live} cleared` : ''}; they are in Frames in view.`);
}
function foldMap() {  // pan, zoom, a new scope or a click on empty map closes the fan and the card
  if (mapCard.contains(document.activeElement)) mapCanvas.focus();
  state.map.fan = null;
  mapShowAll.hidden = true;
  mapCard.hidden = true;
}

// A cleared frame is not in the catalog, so it gets a card beside its point instead of the frame dialog.
let mapCardReturn = null;
function openMapCard(photo, at = toScreen(photo.latitude, photo.longitude), invoker = mapCanvas) {
  const image = document.querySelector('#map-card-thumb');
  if (photo.thumb_url) image.src = photo.thumb_url;
  else image.removeAttribute('src');
  document.querySelector('#map-card-time').textContent = photo.captured_at ? new Date(photo.captured_at).toLocaleString() : 'No capture time';
  document.querySelector('#map-card-place').textContent = photo.place || 'Unnamed place';
  document.querySelector('#map-card-state').textContent = `${MAP_DECISIONS[photo.decision] || 'no decision'} · ${photo.rating ? '★'.repeat(photo.rating) : 'unrated'}`;
  mapCard.hidden = false;
  const { width, height } = mapSize();
  const [w, h] = [mapCard.offsetWidth, mapCard.offsetHeight];
  const left = at.x + 32 + w <= width ? at.x + 32 : at.x - 32 - w;
  mapCard.style.left = `${Math.max(8, Math.min(width - w - 8, left))}px`;
  mapCard.style.top = `${Math.max(8, Math.min(height - h - 8, at.y - h / 2))}px`;
  mapCardReturn = invoker;
  mapCard.focus();
}
function closeMapCard() {
  mapCard.hidden = true;
  (mapCardReturn?.isConnected ? mapCardReturn : mapCanvas).focus();
}
const roundKm = km => (km >= 10 ? Math.round(km) : Number(km.toPrecision(2)));

function drawScale() {
  const scale = document.querySelector('#map-scale');
  const perPixel = kmPerPixel();
  const raw = perPixel * 100;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const nice = [5, 2, 1].map(step => step * magnitude).find(value => value <= raw);
  scale.style.width = `${Math.round(nice / perPixel)}px`;
  scale.textContent = nice >= 1 ? `${nice} km` : `${Math.round(nice * 1000)} m`;
}

// The screen-reader path to the same frames: one button per frame drawn on its own.
// A selection change only moves aria-current, so the focused button survives it.
function renderFramesInView(frames) {
  const photos = [...frames].sort(byFanOrder);  // before the key, so an unchanged set keeps its focused button
  const list = document.querySelector('#map-frames');
  const key = photos.map(mapKey).join(',');
  if (key === state.map.framesKey) {
    list.querySelectorAll('button').forEach(button => {
      if (Number(button.dataset.id) === state.map.selected) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
    });
    return;
  }
  state.map.framesKey = key;
  list.replaceChildren(...photos.map(photo => {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'secondary';
    const when = photo.captured_at ? new Date(photo.captured_at).toLocaleString() : 'No capture time';
    button.textContent = `${photo.place || 'Unnamed place'} · ${when}${photo.cleared ? ' (cleared)' : ''}`;
    if (photo.cleared) {
      button.addEventListener('click', () => openMapCard(photo, undefined, button));
      item.append(button);
      return item;
    }
    button.dataset.id = photo.id;
    if (photo.id === state.map.selected) button.setAttribute('aria-current', 'true');
    button.addEventListener('click', () => openMapFrame(photo.id, button));
    item.append(button);
    return item;
  }));
  if (!photos.length) {
    const item = document.createElement('li');
    item.className = 'quiet';
    item.textContent = state.map.photos.length ? 'Zoom in to see single frames.' : 'No frames with a position.';
    list.append(item);
  }
}

// A frame the contact sheet does not hold narrows the Review filter to its capture day (and to rejects, for a reject),
// so the loupe can open it; the map keeps its view. Closing the loupe returns focus to whatever opened it here.
async function openMapFrame(photoId, invoker = mapCanvas) {
  state.map.selected = photoId;
  drawMap();
  if (!photoById(photoId)) {
    const photo = state.map.photos.find(item => item.id === photoId);
    if (!photo?.captured_at) return announce('That frame has no capture date, so the contact sheet cannot be filtered to it.');
    const day = photo.captured_at.slice(0, 10);
    try {
      await applyFilters(new URLSearchParams({ date_from: day, date_to: day, ...(photo.decision === 'reject' ? { decision: 'reject' } : {}) }).toString());
      await loadMap(false);
    } catch (error) {
      return announce(error.message);
    }
    if (!photoById(photoId)) return announce(`The contact sheet now shows ${day}, but that frame is not on it.`);
    announce(`Review now shows ${day} so the frame can open.`);
  }
  openLoupe(photoId);
  if (openView) viewReturn = invoker;
}

function zoomAbout(x, y, zoom) {
  foldMap();
  const { width, height } = mapSize();
  const before = project(state.map.center.lat, state.map.center.lon, state.map.zoom);
  const under = unproject(before.x + x - width / 2, before.y + y - height / 2, state.map.zoom);
  state.map.zoom = clampZoom(zoom);
  const after = project(under.lat, under.lon, state.map.zoom);
  state.map.center = unproject(after.x - (x - width / 2), after.y - (y - height / 2), state.map.zoom);
  drawMap();
  scheduleFeatures();
}
function panBy(dx, dy) {
  foldMap();
  const centre = project(state.map.center.lat, state.map.center.lon, state.map.zoom);
  state.map.center = unproject(centre.x + dx, centre.y + dy, state.map.zoom);
  drawMap();
  scheduleFeatures();
}
function mapHit(x, y) {
  return state.map.hits.find(hit => Math.hypot(hit.x - x, hit.y - y) <= hit.radius);
}
function activateHit(hit) {
  if (hit.photos.length > 1) return fanOut(hit);
  const [photo] = hit.photos;
  if (photo.cleared) return openMapCard(photo, hit);
  openMapFrame(photo.id);
}

function wireMapInput() {
  const offset = event => {
    const rect = mapCanvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  };
  mapCanvas.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    mapCanvas.setPointerCapture(event.pointerId);
    state.map.drag = { ...offset(event), moved: false };
  });
  mapCanvas.addEventListener('pointermove', event => {
    const drag = state.map.drag;
    if (!drag) return;
    const point = offset(event);
    if (!drag.moved && Math.hypot(point.x - drag.x, point.y - drag.y) < 4) return;
    drag.moved = true;
    panBy(drag.x - point.x, drag.y - point.y);
    Object.assign(drag, point);
  });
  mapCanvas.addEventListener('pointerup', event => {
    const drag = state.map.drag;
    state.map.drag = null;
    if (!drag || drag.moved) return;
    const point = offset(event);
    const hit = mapHit(point.x, point.y);
    if (hit) activateHit(hit);
    else if (state.map.fan || !mapCard.hidden) {
      foldMap();
      drawMap();
    }
  });
  mapCanvas.addEventListener('pointercancel', () => { state.map.drag = null; });
  mapCanvas.addEventListener('wheel', event => {
    event.preventDefault();
    const point = offset(event);
    zoomAbout(point.x, point.y, state.map.zoom - Math.max(-1, Math.min(1, event.deltaY * 0.005)));
  }, { passive: false });
  mapCanvas.addEventListener('dblclick', event => {
    const point = offset(event);
    const hit = mapHit(point.x, point.y);  // empty map or a cluster zooms; a lone frame or a ring slot has opened already
    if (!hit || hit.photos.length > 1) zoomAbout(point.x, point.y, state.map.zoom + (hit ? 2 : 1));
  });
  mapCanvas.addEventListener('keydown', event => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const { width, height } = mapSize();
    const pan = { ArrowLeft: [-64, 0], ArrowRight: [64, 0], ArrowUp: [0, -64], ArrowDown: [0, 64] }[event.key];
    if (pan) panBy(...pan);
    else if (event.key === '+' || event.key === '=') zoomAbout(width / 2, height / 2, state.map.zoom + 1);
    else if (event.key === '-') zoomAbout(width / 2, height / 2, state.map.zoom - 1);
    else if (event.key === '0') fitMap();
    else if (event.key === 'Escape' && (state.map.fan || !mapCard.hidden)) {
      foldMap();
      drawMap();
    } else if (event.key === 'Enter') {
      // Enter opens the selected frame when it is drawn, else whatever sits nearest the centre.
      const hits = state.map.hits;
      const chosen = hits.find(hit => hit.photos.some(photo => photo.id === state.map.selected))
        || [...hits].sort((a, b) => Math.hypot(a.x - width / 2, a.y - height / 2) - Math.hypot(b.x - width / 2, b.y - height / 2))[0];
      if (!chosen) return announce('No frames in view.');
      activateHit(chosen);
    } else return;
    event.preventDefault();
  });
  if (window.ResizeObserver) {
    new window.ResizeObserver(() => {
      if (state.map.fitPending) fitMap();
      else { drawMap(); scheduleFeatures(); }
    }).observe(mapCanvas);
  }
}

const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
function renderOverlays() {
  const list = document.querySelector('#map-overlays');
  list.replaceChildren(...state.map.overlays.map(overlay => {
    const item = document.createElement('li');
    const name = document.createElement('strong');
    name.textContent = overlay.name;
    const detail = document.createElement('span');
    detail.className = 'quiet';
    detail.textContent = overlay.error ? ` unreadable: ${overlay.error}`
      : ` ${plural(overlay.tracks, 'track')}, ${plural(overlay.waypoints, 'waypoint')}${overlay.span ? `, ${overlay.span.map(time => new Date(time).toLocaleString()).join(' to ')}` : ''}${overlay.tracks && !overlay.span ? ', no times, cannot place frames' : ''}`;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'secondary';
    remove.textContent = 'Remove';
    remove.setAttribute('aria-label', `Remove ${overlay.name}`);
    remove.addEventListener('click', async () => {
      try {
        await request(`/api/map/overlays/${encodeURIComponent(overlay.name)}`, { method: 'DELETE' });
        announce(`Removed ${overlay.name} from the map.`);
        await loadMap();
      } catch (error) { announce(error.message); }
    });
    item.append(name, detail, ' ', remove);
    return item;
  }));
  if (!state.map.overlays.length) {
    const item = document.createElement('li');
    item.className = 'quiet';
    item.textContent = 'No overlays yet.';
    list.append(item);
  }
}

async function addOverlayFiles(files) {
  const added = [];
  for (const file of files) {
    if (file.size > 20 * 1024 * 1024) {
      announce(`${file.name} is larger than 20 MB; Lumen did not add it.`);
      continue;
    }
    try {
      const summary = await post('/api/map/overlays', { name: file.name, text: await file.text() });
      added.push(`${summary.name}: ${plural(summary.tracks, 'track')}, ${plural(summary.waypoints, 'waypoint')}`);
    } catch (error) {
      announce(`${file.name}: ${error.message}`);
    }
  }
  if (!added.length) return;
  announce(`Added ${added.join('; ')}.`);
  await loadMap();
}

const geotagField = id => document.querySelector(`#map-geotag-${id}`);
function renderGeotag() {
  const timed = state.map.overlays.filter(overlay => overlay.span);
  document.querySelector('#map-geotag').hidden = !timed.length;
  const unfixed = document.querySelector('#map-unfixed');
  unfixed.hidden = !state.map.unfixed;
  unfixed.textContent = `${state.map.unfixed === 1 ? '1 frame has' : `${state.map.unfixed} frames have`} no position.${timed.length ? '' : ' Add a GPX track to place them.'}`;
  const select = geotagField('track');
  const current = select.value;
  select.replaceChildren(...timed.map(overlay => new Option(overlay.name, overlay.name)));
  if (timed.some(overlay => overlay.name === current)) select.value = current;
  if (timed.length) scheduleGeotagPreview();
}
function geotagBody(dryRun) {  // the form follows the map's Frames radio
  const gap = Number(geotagField('gap').value);
  if (!Number.isInteger(gap) || gap < 1 || gap > 120) throw new Error('Max gap must be a whole number of minutes from 1 to 120.');
  const offset = geotagField('offset');
  if (!offset.checkValidity()) throw new Error('Camera clock offset must look like +02:00.');
  return { query: state.map.scope === 'shown' ? location.search.slice(1) : '', offset: offset.value.trim() || null, max_gap_minutes: gap, dry_run: dryRun,
    replace: geotagField('replace').checked };
}
// Each call takes a ticket; only the newest writes the server's offset back, and only over the value it sent.
let geotagRequest = 0;
async function geotag(dryRun, ticket = ++geotagRequest) {
  const body = geotagBody(dryRun);
  const result = await post(`/api/map/overlays/${encodeURIComponent(geotagField('track').value)}/geotag`, body);
  const offset = geotagField('offset');
  if (ticket === geotagRequest && offset.value.trim() === (body.offset ?? '')) offset.value = result.offset;
  return result;
}
let geotagTimer = null;
function scheduleGeotagPreview() {
  window.clearTimeout(geotagTimer);
  geotagTimer = window.setTimeout(async () => {
    const preview = geotagField('preview');
    const ticket = ++geotagRequest;
    try {
      const result = await geotag(true, ticket);
      if (ticket === geotagRequest) preview.textContent = `${plural(result.placed, 'frame')} would be placed, ${result.out_of_span} outside the track`;
    } catch (error) {
      if (ticket === geotagRequest) preview.textContent = error.message;
    }
  }, 300);
}

wireMapInput();
mapShowAll.addEventListener('click', async () => {
  const ids = (state.map.fan?.members || []).filter(photo => !photo.cleared).slice(0, 200).map(photo => photo.id);
  try {  // the default filter hides rejects, and a rejected member is still one of these frames
    await applyFilters(new URLSearchParams({ ids: ids.join(','), show_rejected: '1' }).toString());
  } catch (error) {
    return announce(error.message);
  }
  selectTab(document.querySelector('#review-tab'));
  announce(`Review shows the ${plural(ids.length, 'frame')} from the map; Reset clears it.`);
});
document.querySelector('#map-card-close').addEventListener('click', closeMapCard);
mapCard.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  event.preventDefault();
  closeMapCard();
});
document.querySelectorAll('input[name="map-scope"]').forEach(radio => radio.addEventListener('change', () => loadMap()));
document.querySelector('#map-overlay-file').addEventListener('change', async event => {
  await addOverlayFiles([...event.target.files]);
  event.target.value = '';
});
const mapPanel = document.querySelector('#map-panel');
mapPanel.addEventListener('dragover', event => event.preventDefault());
mapPanel.addEventListener('drop', event => {
  event.preventDefault();
  addOverlayFiles([...event.dataTransfer.files]);
});
['track', 'offset', 'gap', 'replace'].forEach(id => geotagField(id).addEventListener('change', scheduleGeotagPreview));
['offset', 'gap'].forEach(id => geotagField(id).addEventListener('input', scheduleGeotagPreview));
document.querySelector('#map-geotag-form').addEventListener('submit', async event => {
  event.preventDefault();
  const track = geotagField('track').value;
  window.clearTimeout(geotagTimer);
  try {
    const result = await geotag(false);  // its ticket outdates any dry run still in flight
    announce(`Placed ${plural(result.placed, 'frame')} on ${track}; ${result.out_of_span} fell outside the track.`);
    await loadMap(false);  // reloads the frames and schedules a fresh preview
  } catch (error) { announce(error.message); }
});

// From the frame dialog: close it, open the Map tab centred on this frame at street zoom, the frame marked.
function showOnMap(photo, record) {
  closeView();
  state.map.selected = photo.id;
  state.map.focus = { lat: record.latitude, lon: record.longitude };
  selectTab(document.querySelector('#map-tab'));
  mapCanvas.focus();
}

// One [role=tablist]: click, Arrow keys, Home, and End select a tab, show its panel, and remember it under storageKey.
// A click on the tab that is already selected calls onReselect instead, when given.
function wireTablist(selector, storageKey, onSelect = () => {}, onReselect = null) {
  const tabs = Array.from(document.querySelectorAll(`${selector} [role="tab"]`));
  const select = tab => {
    tabs.forEach(item => {
      const selected = item === tab;
      item.setAttribute('aria-selected', String(selected));
      item.tabIndex = selected ? 0 : -1;
      document.querySelector(`#${item.getAttribute('aria-controls')}`).hidden = !selected;
    });
    try { localStorage.setItem(storageKey, tab.id); } catch { /* per-visit only */ }
    onSelect(tab);
  };
  tabs.forEach(tab => {
    tab.addEventListener('click', () => (onReselect && tab.getAttribute('aria-selected') === 'true' ? onReselect(tab) : select(tab)));
    tab.addEventListener('keydown', event => {
      const usable = tabs.filter(item => item.getAttribute('aria-disabled') !== 'true');  // an unavailable tab is still selectable by click, so its reason can be read
      const at = usable.indexOf(tab);  // -1 while an unavailable tab holds the focus: step from where it sits among all the tabs
      const passed = usable.filter(item => tabs.indexOf(item) < tabs.indexOf(tab)).length;
      const last = usable.length - 1;
      const next = {
        ArrowRight: (at < 0 ? passed : at + 1) % usable.length,
        ArrowLeft: ((at < 0 ? passed : at) + last) % usable.length,
        Home: 0,
        End: last,
      }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      select(usable[next]);
      usable[next].focus();
    });
  });
  // A removed or unknown remembered id keeps the current tab.
  select.restore = () => {
    let id = null;
    try { id = localStorage.getItem(storageKey); } catch { /* per-visit only */ }
    const tab = tabs.find(item => item.id === id);
    if (tab) select(tab);
  };
  return select;
}

const selectTab = wireTablist('.tab-row', 'lumen-tab', tab => {
  collapsePanel(false);  // choosing a tab always shows its panel
  const main = document.querySelector('#main-content');
  // The map stage grows from nothing once its panel shows. Safari abandons a smooth scroll when the content
  // above it changes size, and a plain scrollTo issued while that animation is in flight loses to it, so the
  // Map tab jumps instantly instead, with the top asserted once more after the map has sized and drawn.
  main.scrollTo({ top: 0, behavior: tab.id === 'map-tab' ? 'auto' : scrollMotion() });  // the panel sits above the sheet
  if (tab.id === 'review-tab') loadRejectSuggestions();
  if (tab.id === 'assist-tab') loadVocabulary();
  if (tab.id === 'clear-tab') loadCleared();
  if (tab.id === 'map-tab') loadMap().finally(() => main.scrollTo({ top: 0, behavior: 'auto' }));
}, () => togglePanel());  // clicking the open tab again folds the panel away, and back

function collapsePanel(collapsed) {
  document.querySelector('#workflow-panels').hidden = collapsed;
  try {
    if (collapsed) localStorage.setItem('lumen-panel-collapsed', '1');
    else localStorage.removeItem('lumen-panel-collapsed');
  } catch { /* per-visit only */ }
}
function togglePanel() {
  const collapsed = !document.querySelector('#workflow-panels').hidden;
  collapsePanel(collapsed);
  announce(collapsed ? 'Workflow panel collapsed; the contact sheet has the space.' : 'Workflow panel shown.');
}
try {
  const folded = localStorage.getItem('lumen-panel-collapsed') === '1';  // read first: restoring a tab shows its panel and would clear the fold
  selectTab.restore();  // before load(), so the first load fills the right panel; a removed tab stays on Add photos
  if (folded) collapsePanel(true);
} catch { /* per-visit only */ }
// A fresh visit (no remembered tab and no remembered fold) starts with the panel folded, so an empty catalog shows the
// first-run card and a full one shows the sheet; Add photos is selected in the markup. A fold made before a reload stays.
function chooseDefaultTab() {
  let remembered = null;
  try { remembered = localStorage.getItem('lumen-tab') ?? localStorage.getItem('lumen-panel-collapsed'); } catch { /* per-visit only */ }
  if (remembered === null) collapsePanel(true);
}
// Add photos holds two sections; the radios, not the tablist, show one of them.
const addSourceRadios = [...document.querySelectorAll('input[name="add-source"]')];
function showAddSource(value) {
  document.querySelector('#ingest-panel').hidden = value !== 'ingest';
  document.querySelector('#import-panel').hidden = value !== 'import';
}
addSourceRadios.forEach(radio => radio.addEventListener('change', () => {
  showAddSource(radio.value);
  try { localStorage.setItem('lumen-add-source', radio.value); } catch { /* per-visit only */ }
}));
try {
  const source = addSourceRadios.find(radio => radio.value === localStorage.getItem('lumen-add-source'));
  if (source) { addSourceRadios.forEach(radio => { radio.checked = radio === source; }); showAddSource(source.value); }
} catch { /* per-visit only */ }

document.querySelector('#jump-selected').addEventListener('click', () => {
  const index = state.groups.findIndex(group => group.id === state.selectedGroupId);
  if (index < 0) return announce('No burst is selected.');
  announce(scrollToGroup(state.selectedGroupId) ? `Burst ${index + 1} shown.` : `Burst ${index + 1} has no frames on the contact sheet.`);
});
document.querySelector('#back-to-top').addEventListener('click', () => {
  document.querySelector('#main-content').scrollTo({ top: 0, behavior: scrollMotion() });
  announce('Scrolled to the workflow tabs.');
});
// Keep scroll-into-view clear of the sticky tab row and, where it sticks, the contact sheet's filter bar beneath it.
if (window.ResizeObserver) {
  const top = document.querySelector('.main-top');
  const filters = document.querySelector('.contact-top');
  const observer = new window.ResizeObserver(() => {
    const pinned = window.getComputedStyle(filters).position === 'sticky' ? filters.offsetHeight : 0;
    document.documentElement.style.setProperty('--main-top', `${top.offsetHeight}px`);
    document.documentElement.style.setProperty('--sticky-top', `${top.offsetHeight + pinned}px`);
  });
  observer.observe(top);
  observer.observe(filters);
}
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
// Every filter applies as soon as it changes; the text fields debounce below, and Enter in them applies at once.
[...Object.values(FILTER_CONTROLS).filter(selector => selector !== '#filter-text'), ...Object.keys(STATUSES).map(value => `#status-${value}`)]
  .forEach(selector => document.querySelector(selector).addEventListener('change', () => applyFilters().catch(error => announce(error.message))));
document.querySelector('#filter-form').addEventListener('submit', event => {
  event.preventDefault();
  cancelFind();
  return applyFilters().catch(error => announce(error.message));
});
// The rail find and the form's tag text are one `text` filter; typing in either mirrors the other and applies it.
[['#find', '#filter-text'], ['#filter-text', '#find']].forEach(([source, mirror]) => document.querySelector(source).addEventListener('input', () => {
  document.querySelector(mirror).value = document.querySelector(source).value;
  updateExpandSearch();
  window.clearTimeout(findTimer);
  findTimer = window.setTimeout(() => {
    findTimer = null;
    applyFilters().catch(error => announce(error.message));
  }, 300);
}));
// Advisory: the fast model adds vocabulary keywords to the search as OR terms; the photographer can edit or clear them.
function updateExpandSearch() {
  const blank = ['#find', '#filter-text'].every(selector => !document.querySelector(selector).value.trim());
  document.querySelector('#expand-search').disabled = state.busy || blank || !roleReady('fast');
}
document.querySelector('#expand-search').addEventListener('click', async () => {
  const query = (document.querySelector('#filter-text').value || document.querySelector('#find').value).trim();
  state.busy = true;
  updateWorkflow();
  try {
    const result = await aiCall('Search expansion', () => post('/api/search/expand', { query }));
    cancelFind();
    document.querySelector('#filter-text').value = result.text;
    document.querySelector('#find').value = result.text;
    await applyFilters();
    announce(`Expanded to ${result.terms.length + 1} term${result.terms.length ? 's' : ''}`);
  } catch (error) {
    announce(error.message);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
});
document.querySelector('#reset-filters').addEventListener('click', async () => {
  cancelFind();
  await applyFilters('').catch(error => announce(error.message));
});
document.querySelector('#show-more').addEventListener('click', showMore);
// Tile density: a CSS variable on the sheet; medium is the stylesheet default, the choice is remembered per browser.
const TILE_SIZES = { small: '140px', medium: null, large: '260px' };
function applyTileSize(size, announceChange = false) {
  const select = document.querySelector('#tile-size');
  select.value = size;
  if (TILE_SIZES[size]) sheet.style.setProperty('--tile-min', TILE_SIZES[size]);
  else sheet.style.removeProperty?.('--tile-min');
  try { localStorage.setItem('lumen-tile-size', size); } catch { /* per-visit only */ }
  if (announceChange) announce(`${size[0].toUpperCase()}${size.slice(1)} tiles`);
}
document.querySelector('#tile-size').addEventListener('change', () => applyTileSize(document.querySelector('#tile-size').value, true));
try {
  const rememberedSize = localStorage.getItem('lumen-tile-size');
  if (rememberedSize in TILE_SIZES && rememberedSize !== 'medium') applyTileSize(rememberedSize);
} catch { /* per-visit only */ }
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

// Compare and loupe: modal views over the sheet; culling keys act on the focused pane, Escape closes.
const SIDES = ['left', 'right'];
const paneMetrics = new Map();
let comparing = [];  // photo ids shown left, right
let openView = null;
let viewOrigin = null;
let viewReturn = null;  // an element outside the sheet (the map) that gets focus back when the view closes
let syncing = null;  // the viewport whose scroll we just set; its echo is ignored

function showView(dialog) {
  openView = dialog;
  viewOrigin = state.focusedId;
  viewReturn = null;
  if (dialog.showModal) dialog.showModal();
  else dialog.hidden = false;
}

function closeView() {
  const dialog = openView;
  if (!dialog) return;
  saveDevelop();  // leaving the panel saves; the RAW is untouched either way
  openView = null;
  comparing = [];
  if (dialog.close) dialog.close();
  else dialog.hidden = true;
  const back = viewReturn;
  viewReturn = null;
  const rebuilt = back?.dataset.id && document.querySelector(`#map-frames button[data-id="${back.dataset.id}"]`);  // the list re-rendered meanwhile
  if (back) (back.isConnected ? back : rebuilt || mapCanvas).focus();
  else focusFrame(viewOrigin);
}

// Scale 1 of the stored preview in both compare panes, so zoom matches; preview_url carries the version query.
// fit leaves the size to CSS, which fits the frame view's preview whole into its viewport.
function showPreview(viewport, photo, src = photo.preview_url, loaded = () => {}, fit = false) {
  const image = document.createElement('img');
  image.alt = photo.name;
  image.draggable = false;  // a pointer drag draws a crop, never drags the file out
  if (fit) image.className = 'fit';
  image.addEventListener('load', () => {
    if (!fit) Object.assign(image, { width: image.naturalWidth, height: image.naturalHeight });
    loaded(image);
  });
  image.src = src;
  viewport.setAttribute('aria-label', `${photo.name} viewport`);
  viewport.replaceChildren(image);
  return image;
}

function syncScroll(from, to) {
  if (syncing === from) { syncing = null; return; }
  const left = from.scrollLeft / Math.max(1, from.scrollWidth - from.clientWidth) * (to.scrollWidth - to.clientWidth);
  const top = from.scrollTop / Math.max(1, from.scrollHeight - from.clientHeight) * (to.scrollHeight - to.clientHeight);
  if (Math.abs(to.scrollLeft - left) < 1 && Math.abs(to.scrollTop - top) < 1) return;
  syncing = to;
  to.scrollLeft = left;
  to.scrollTop = top;
}

const PAN = { ArrowLeft: [-40, 0], ArrowRight: [40, 0], ArrowUp: [0, -40], ArrowDown: [0, 40] };
function panKey(event, viewport) {
  if (!(event.key in PAN) && !['Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) return;
  event.stopPropagation();  // scroll the viewport, never navigate the sheet behind it
  if (!(event.key in PAN)) return;
  event.preventDefault();
  viewport.scrollLeft += PAN[event.key][0];
  viewport.scrollTop += PAN[event.key][1];
}

function percent(value) {
  return `${Math.round(value * 100)}%`;
}

function refreshPanes(photoId) {
  comparing.forEach((id, index) => {
    if (id !== photoId) return;
    const side = SIDES[index];
    const photo = photoById(id);
    const metrics = paneMetrics.get(id);
    const rows = [['Name', photo.name], ['Overall score', percent(photo.total_score)], ['Sharpness', percent(metrics.sharpness)], ['Exposure', percent(metrics.exposure)],
      ...(metrics.eye_quality == null ? [] : [['Eye quality', percent(metrics.eye_quality)]]),
      ['Decision', DECISION_WORDS[photo.decision]], ['Rating', photo.rating ? `${photo.rating} of 5` : 'No rating']];
    document.querySelector(`#compare-${side}-details`).replaceChildren(...rows.flatMap(([term, value]) => {
      const dt = document.createElement('dt');
      dt.textContent = term;
      const dd = document.createElement('dd');
      dd.textContent = value;
      return [dt, dd];
    }));
    const rating = document.querySelector(`#compare-${side}-rating`);
    rating.value = String(photo.rating);
    rating.setAttribute('aria-label', `Rating for ${photo.name}`);
  });
}

function fillPanes() {
  comparing.forEach((id, index) => {
    const photo = photoById(id);
    document.querySelector(`#compare-${SIDES[index]}`).setAttribute('aria-label', `${index ? 'Right' : 'Left'}: ${photo.name}`);
    showPreview(document.querySelector(`#compare-${SIDES[index]}-viewport`), photo);
    refreshPanes(id);
  });
}

async function openCompare() {
  if (state.compare.length !== 2 || openView) return;
  if (state.cullingBlocked) return announce(cullingBlockedText());
  const ids = [...state.compare];
  try {
    await Promise.all(ids.map(async id => paneMetrics.set(id, (await request(`/api/photos/${id}`)).metrics)));
  } catch (error) {
    return announce(error.message);
  }
  if (!ids.every(photoById)) return;
  comparing = ids;
  fillPanes();
  showView(document.querySelector('#compare'));
  document.querySelector('#compare-left-viewport').focus();
}

// Re-rendering or disabling the focused control drops focus to <body>, outside the dialog's culling listener.
function keepFrameViewFocus() {
  if (openView?.id === 'loupe' && !openView.contains(document.activeElement)) loupeViewport.focus();
}

function openLoupe(photoId) {
  const photo = photoById(photoId);
  if (openView || !photo) return;
  if (state.cullingBlocked) return announce(cullingBlockedText());
  showFrame(photo);
  showView(document.querySelector('#loupe'));
  loupeViewport.focus();
}

// The frame view selects the frame as a sheet click does, then gathers its inspector and crops at preview size.
function showFrame(photo) {
  if (state.develop.photoId !== photo.id) saveDevelop();  // the frame being left keeps its settings
  document.querySelector('#loupe-heading').textContent = photo.name;
  selectFrameTab.restore();
  showPreview(loupeViewport, photo, undefined, () => drawCropOverlay(shownCropRect()), true);
  cropDrag = null;
  state.manualCrop = null;
  drawCropOverlay(null);
  Object.assign(fullButton, { disabled: false, textContent: 'Full size' });
  fullButton.setAttribute('aria-pressed', 'false');
  inspect(photo).catch(error => announce(error.message));  // sets selectedId before its first await, so the crops below are this frame's
  loadComposition();
  if (document.querySelector('#exif-tab').getAttribute('aria-selected') === 'true') loadExif(photo.id);  // a step keeps the open tab current
  const ids = Array.from(document.querySelectorAll('.frame'), card => Number(card.dataset.id));
  announce(`${photo.name}, ${ids.indexOf(photo.id) + 1} of ${ids.length}`);
}

// Read from the original on demand, so the catalog stores none of it; a newer request (a step) wins.
let exifRequest = 0;
async function loadExif(photoId) {
  const requestId = ++exifRequest;
  const list = document.querySelector('#exif-fields');
  const status = document.querySelector('#exif-status');
  list.replaceChildren();
  status.textContent = photoId ? 'Reading EXIF…' : '';
  if (!photoId) return;
  try {
    const { fields } = await request(`/api/photos/${photoId}/exif`);
    if (requestId !== exifRequest) return;
    list.replaceChildren(...fields.flatMap(({ label, value }) => {
      const term = document.createElement('dt');
      term.textContent = label;
      const detail = document.createElement('dd');
      detail.textContent = value;
      return [term, detail];
    }));
    status.textContent = fields.length ? '' : 'No EXIF in this file.';
  } catch (error) {
    if (requestId === exifRequest) status.textContent = error.message;
  }
}

// Swap between the stored preview and the camera's embedded full-size JPEG; the button names the real pixel size.
function toggleFullSize() {
  const photo = photoById(state.selectedId);
  if (!photo || fullButton.disabled) return;
  cropDrag = null;
  const redrawCrop = () => drawCropOverlay(shownCropRect());
  if (developOwnsFullSize()) {
    const zoom = fullButton.getAttribute('aria-pressed') !== 'true';
    fullButton.setAttribute('aria-pressed', String(zoom));
    announce(zoom ? 'Full size, rendering from the raw' : 'Preview size');
    return renderDevelopPreview();
  }
  if (fullButton.getAttribute('aria-pressed') === 'true') {
    showPreview(loupeViewport, photo, undefined, redrawCrop, true);
    fullButton.textContent = 'Full size';
    fullButton.setAttribute('aria-pressed', 'false');
    return announce('Preview size');
  }
  fullButton.setAttribute('aria-pressed', 'true');  // a second z while it loads returns to the preview
  state.develop.zoomShown = false;  // this branch shows the camera's JPEG, not a develop render
  const image = showPreview(loupeViewport, photo, `/full/${photo.id}?${photo.preview_url.split('?')[1] || ''}`, loaded => {
    if (loupeViewport.children[0] !== image) return;  // stepped on or toggled off while it loaded
    redrawCrop();
    fullButton.textContent = `Full size, ${loaded.naturalWidth} by ${loaded.naturalHeight}`;
    loupeViewport.scrollLeft = (loupeViewport.scrollWidth - loupeViewport.clientWidth) / 2;
    loupeViewport.scrollTop = (loupeViewport.scrollHeight - loupeViewport.clientHeight) / 2;
    announce(fullButton.textContent);
  });
  image.addEventListener('error', async () => {
    if (loupeViewport.children[0] !== image) return;
    const preview = showPreview(loupeViewport, photo, undefined, redrawCrop, true);
    fullButton.disabled = true;
    fullButton.setAttribute('aria-pressed', 'false');
    keepFrameViewFocus();
    const offline = await fetch(image.src).then(response => response.status === 503, () => false);  // the img error carries no status
    if (loupeViewport.children[0] !== preview) return;  // stepped on while asking
    fullButton.textContent = offline ? 'original is offline' : 'no full-size image in this file';
    announce(fullButton.textContent);
  });
}

// Your own crop: a rect normalised to the shown image's natural size, so preview and full size agree.
const CROP_LOCKS = { free: null, '3:2': 3 / 2, '2:3': 2 / 3, '1:1': 1, '4:5': 4 / 5 };
const CROP_MIN_DISPLAY = 24;  // display px on either side; smaller drags are slips
let cropDrag = null;  // { image, x, y, rect } in natural px while the pointer is down

function cropLockValue() {
  return cropLock.value in CROP_LOCKS ? cropLock.value : 'free';
}

function cropPoint(event, image) {
  const box = image.getBoundingClientRect();
  const clamp = (value, max) => Math.max(0, Math.min(max, value));
  return [clamp((event.clientX - box.left) * image.naturalWidth / box.width, image.naturalWidth),
    clamp((event.clientY - box.top) * image.naturalHeight / box.height, image.naturalHeight)];
}

function cropDragRect(event) {
  const { image, x, y } = cropDrag;
  const [width, height] = [image.naturalWidth, image.naturalHeight];
  const [px, py] = cropPoint(event, image);
  let w = Math.abs(px - x);
  let h = Math.abs(py - y);
  const ratio = CROP_LOCKS[cropLockValue()];
  if (ratio) {  // the height follows the width, shrinking both if it would leave the image
    const roomY = py < y ? y : height - y;
    h = w / ratio;
    if (h > roomY) [h, w] = [roomY, roomY * ratio];
  }
  const left = px < x ? x - w : x;
  const top = py < y ? y - h : y;
  return { left: left / width, top: top / height, width: w / width, height: h / height };
}

// The overlay covers the image's rendered box, so it scrolls with it; showPreview's replaceChildren detaches it.
function drawCropOverlay(rect) {
  const image = loupeViewport.children[0];
  cropOverlay.hidden = !rect;
  if (!rect || !image) return;
  if (!loupeViewport.contains(cropOverlay)) loupeViewport.append(cropOverlay);
  const box = image.getBoundingClientRect();
  Object.assign(cropOverlay.style, { width: `${box.width}px`, height: `${box.height}px` });
  Object.assign(cropOutline.style, { left: `${rect.left * 100}%`, top: `${rect.top * 100}%`, width: `${rect.width * 100}%`, height: `${rect.height * 100}%` });
}

// A 5 s amber ring says the frame view now shows a different photo; removed by timer so reduced motion gets it too.
let flashTimer = null;
function flashFrameView() {
  loupeViewport.classList.remove('pick-flash');
  void loupeViewport.offsetWidth;  // restart the animation on a second switch
  loupeViewport.classList.add('pick-flash');
  window.clearTimeout(flashTimer);
  flashTimer = window.setTimeout(() => loupeViewport.classList.remove('pick-flash'), 5000);
}

// Filmstrip: step the loupe through the rendered frames in sheet order.
function stepLoupe(delta) {
  const ids = Array.from(document.querySelectorAll('.frame'), card => Number(card.dataset.id));
  const index = ids.indexOf(state.focusedId) + delta;
  if (index < 0) return announce('First frame');
  if (index >= ids.length) return announce('Last frame');
  const photo = photoById(ids[index]);
  if (!photo) return;
  focusFrame(photo.id, false);
  showFrame(photo);
  viewOrigin = photo.id;
}

for (const name of ['#compare', '#loupe']) {
  const dialog = document.querySelector(name);
  dialog.addEventListener('keydown', handleCullingKey);
  dialog.addEventListener('close', closeView);  // native Escape from a select or button
}
const loupeViewport = document.querySelector('#loupe-viewport');
const fullButton = document.querySelector('#loupe-full');
const cropOverlay = document.querySelector('#crop-overlay');
const cropOutline = document.querySelector('#crop-outline');
const cropLock = document.querySelector('#crop-lock');
const selectFrameTab = wireTablist('#frame-tabs', 'lumen-frame-tab', tab => {
  if (tab.id === 'develop-tab') return renderDevelopPreview();  // the panel's render is also the histogram's source
  if (tab.id === 'exif-tab') loadExif(state.selectedId);
  // Only a develop render needs handing back. The embedded JPEG at full size belongs to the
  // Frame and Crop tabs, and resetting that would throw away a zoom the photographer set.
  if (!state.develop.zoomShown) return;
  const photo = photoById(state.selectedId);
  if (!photo) return;
  fullButton.setAttribute('aria-pressed', 'false');
  fullButton.textContent = 'Full size';
  state.develop.zoomShown = false;
  showPreview(loupeViewport, photo, undefined, () => drawCropOverlay(shownCropRect()), true);
});
loupeViewport.addEventListener('keydown', event => {
  if (event.key === 'Escape' && cropDrag) {  // cancel the drag; the dialog never sees this Escape
    event.preventDefault();
    event.stopPropagation();
    cropDrag = null;
    drawCropOverlay(shownCropRect());
    return announce('Crop cancelled');
  }
  // An image that fits has nothing to pan sideways, so Left and Right go on to step the frame view.
  if ((event.key === 'ArrowLeft' || event.key === 'ArrowRight') && !(loupeViewport.scrollWidth > loupeViewport.clientWidth)) return;
  panKey(event, loupeViewport);
}, { capture: true });
loupeViewport.addEventListener('pointerdown', event => {
  const image = loupeViewport.children[0];
  if (event.button !== 0 || !(event.target === image || event.target === cropOutline) || !image.naturalWidth) return;
  event.preventDefault();
  loupeViewport.focus({ preventScroll: true });  // Escape then cancels the drag rather than closing the dialog
  loupeViewport.setPointerCapture(event.pointerId);
  const [x, y] = cropPoint(event, image);
  const shown = shownCropRect();  // a press inside the outline moves it; anywhere else, or with Shift held, draws a new one
  const inside = shown && !event.shiftKey && x >= shown.left * image.naturalWidth && x <= (shown.left + shown.width) * image.naturalWidth
    && y >= shown.top * image.naturalHeight && y <= (shown.top + shown.height) * image.naturalHeight;
  cropDrag = { image, x, y, rect: null, move: inside ? shown : null };
});
// Moving keeps the size and slides the rect, stopping at the image edges.
function cropMoveRect(event) {
  const { image, x, y, move } = cropDrag;
  const [px, py] = cropPoint(event, image);
  const clamp = (value, max) => Math.max(0, Math.min(max, value));
  return { ...move, left: clamp(move.left + (px - x) / image.naturalWidth, 1 - move.width), top: clamp(move.top + (py - y) / image.naturalHeight, 1 - move.height) };
}
loupeViewport.addEventListener('pointermove', event => {
  if (!cropDrag) return;
  cropDrag.rect = cropDrag.move ? cropMoveRect(event) : cropDragRect(event);
  drawCropOverlay(cropDrag.rect);
});
loupeViewport.addEventListener('pointerup', event => {
  if (!cropDrag) return;
  const { image, move } = cropDrag;
  const rect = move ? cropMoveRect(event) : cropDragRect(event);
  cropDrag = null;
  const box = image.getBoundingClientRect();
  const slip = move
    ? Math.hypot((rect.left - move.left) * box.width, (rect.top - move.top) * box.height) < CROP_MIN_DISPLAY  // a press on the outline is not a move
    : rect.width * box.width < CROP_MIN_DISPLAY || rect.height * box.height < CROP_MIN_DISPLAY;
  if (slip) return drawCropOverlay(shownCropRect());
  const aspect = move ? (state.manualCrop?.aspect ?? state.composition?.active_crop?.aspect ?? 'free') : cropLockValue();
  state.manualCrop = { rect, aspect };
  drawCropOverlay(rect);
  if (state.composition?.photo_id === state.selectedId) renderComposition();
  selectFrameTab(document.querySelector('#crop-tab'));  // the card, lock, and save button live there
  if (move) return announce(`Crop moved to ${Math.round(rect.left * image.naturalWidth)}, ${Math.round(rect.top * image.naturalHeight)}`);
  const lock = cropLockValue() === 'free' ? '' : `, ${cropLockValue()}`;
  announce(`Crop drawn${lock}: ${Math.round(rect.width * image.naturalWidth)} by ${Math.round(rect.height * image.naturalHeight)} pixels`);
});
if (window.ResizeObserver) new window.ResizeObserver(() => drawCropOverlay(cropDrag?.rect ?? shownCropRect())).observe(loupeViewport);
loupeViewport.addEventListener('pointercancel', () => {
  if (!cropDrag) return;
  cropDrag = null;
  drawCropOverlay(shownCropRect());
});
cropLock.addEventListener('change', () => {
  try { localStorage.setItem('lumen-crop-lock', cropLockValue()); } catch { /* per-visit only */ }
});
try {
  const rememberedLock = localStorage.getItem('lumen-crop-lock');
  if (CROP_LOCKS[rememberedLock]) cropLock.value = rememberedLock;
} catch { /* per-visit only */ }
fullButton.addEventListener('click', toggleFullSize);
document.querySelector('#loupe-export').addEventListener('click', () => exportCurrentFrame());

// The Develop tab. Only the fields the photographer actually moved are ever sent: Apple's raw pipeline seeds the
// neutral white balance and the baseline noise reduction from each file, so "not set" and "set to the default" are
// different states, and Reset deletes a key rather than guessing the value it would have had.
// min and max repeat the server's own ranges, which the markup also carries; a test holds the three in step.
const DEVELOP_CONTROLS = [
  { key: 'exposure_ev', label: 'Exposure', unit: ' EV', min: -3, max: 3, park: 0, decimals: 2 },
  { key: 'temperature_k', label: 'Temperature', unit: ' K', min: 2000, max: 12000, park: 5500, decimals: 0 },
  { key: 'tint', label: 'Tint', unit: '', min: -150, max: 150, park: 0, decimals: 0 },
  { key: 'contrast', label: 'Contrast', unit: '', min: 0, max: 1, park: 0.5, decimals: 2 },
  { key: 'sharpness', label: 'Sharpness', unit: '', min: 0, max: 1, park: 0.5, decimals: 2 },
  { key: 'luminance_nr', label: 'Luminance noise reduction', unit: '', min: 0, max: 1, park: 0.5, decimals: 2 },
  { key: 'color_nr', label: 'Colour noise reduction', unit: '', min: 0, max: 1, park: 0.5, decimals: 2 },
  { key: 'clarity', label: 'Clarity', unit: '', min: 0, max: 1, park: 0, decimals: 2 },
];
const CURVE_IDENTITY = [[0, 0], [0.25, 0.25], [0.5, 0.5], [0.75, 0.75], [1, 1]];
const CURVE_GAP = 0.01;  // the server rejects equal x values, so a dragged point stops short of its neighbour
const [CURVE_W, CURVE_H] = [100, 60];  // the viewBox in index.html

const developNodes = {};
function developNode(id) {
  return developNodes[id] ??= document.querySelector(`#${id}`);
}

function developClamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

function developCurve() {
  return state.develop.fields.curve || CURVE_IDENTITY;
}

function curveText(index) {
  const [x, y] = developCurve()[index];
  return `point ${index + 1}: input ${Math.round(x * 100)}%, output ${Math.round(y * 100)}%`;
}

// Y is Rec. 709 luma, R, G and B the display channels. Shown channels are a preference that
// outlives the frame, so it is not reset with the per-frame develop state.
const HISTOGRAM_CHANNELS = ['y', 'r', 'g', 'b'];
const shownChannels = { y: true, r: true, g: true, b: true };

// One stepped path per channel, in the viewBox's own units.
function histogramPath(buckets, peak) {
  const step = CURVE_W / Math.max(1, buckets.length);
  return buckets.map((value, index) => {
    const [left, right] = [index * step, (index + 1) * step];
    const top = CURVE_H * (1 - developClamp(value / peak, 0, 1));
    return `M${left.toFixed(2)},${CURVE_H}L${left.toFixed(2)},${top.toFixed(2)}L${right.toFixed(2)},${top.toFixed(2)}L${right.toFixed(2)},${CURVE_H}Z`;
  }).join('');
}

// One widget: the channel bands as stepped paths behind the curve, both in the viewBox's own units.
function drawDevelopWidget() {
  const histogram = state.develop.histogram || {};
  // One scale for all four, taken over every channel whether shown or not: the bands are
  // counts of the same pixels, so a shared scale is what makes their heights comparable, and
  // a fixed one means toggling a channel does not silently rescale the three left behind.
  //
  // The end buckets are left out of it. Clipped pixels all land in the first or last bucket,
  // and on a frame that clips — a sunset against a black silhouette — that one bucket ran
  // several times the tallest real bucket, which drew the entire distribution as a flat line
  // along the floor. Scaled to the tallest bucket that is not an end, the shape is readable
  // and the end bars clamp to full height, which is what clipping should look like.
  // The margin is a share of the tonal range, not a bucket count: clipping occupies the top
  // and bottom couple of percent of display values however finely they are binned, so a fixed
  // one bucket per end stopped excluding the spike as soon as the resolution rose.
  const width = Math.max(...HISTOGRAM_CHANNELS.map(channel => (histogram[channel] || []).length), 0);
  const margin = Math.max(1, Math.round(width * 0.02));
  const inner = HISTOGRAM_CHANNELS.flatMap(channel => (histogram[channel] || []).slice(margin, -margin));
  const scale = inner.length ? inner : HISTOGRAM_CHANNELS.flatMap(channel => histogram[channel] || []);
  const peak = Math.max(1e-9, ...scale);
  HISTOGRAM_CHANNELS.forEach(channel => {
    const buckets = shownChannels[channel] ? histogram[channel] || [] : [];
    developNode(`develop-histogram-${channel}`).setAttribute('d', histogramPath(buckets, peak));
  });
  const place = ([x, y]) => `${(x * CURVE_W).toFixed(2)},${((1 - y) * CURVE_H).toFixed(2)}`;
  developNode('develop-curve-line').setAttribute('d', developCurve().map((point, index) => `${index ? 'L' : 'M'}${place(point)}`).join(''));
  developCurve().forEach((point, index) => {
    const node = developNode(`develop-point-${index}`);
    node.setAttribute('cx', (point[0] * CURVE_W).toFixed(2));
    node.setAttribute('cy', ((1 - point[1]) * CURVE_H).toFixed(2));
    node.setAttribute('aria-valuenow', String(Math.round(point[1] * 100)));
    node.setAttribute('aria-valuetext', curveText(index));
  });
}

// White balance parks on this frame's own neutral, which the last render reported; the amounts park mid-range,
// because the file's baseline for them is not reported and must not be invented.
// The Develop tab renders its own full size, so the button means something different there:
// the camera's embedded JPEG shows none of the photographer's settings.
function developOwnsFullSize() {
  return state.develop.ready && developNode('develop-tab').getAttribute('aria-selected') === 'true';
}

function developZoomed() {
  return developOwnsFullSize() && fullButton.getAttribute('aria-pressed') === 'true';
}

function developPark(control) {
  const neutral = { temperature_k: state.develop.neutral?.temperature, tint: state.develop.neutral?.tint }[control.key];
  if (Number.isFinite(neutral)) return neutral;
  // The decoder's own baseline for this file, which is not the middle of the slider: on a
  // Sony raw sharpness already decodes at 1.0, so a handle parked at 0.5 invited the
  // photographer to sharpen further when the only direction available is down.
  const decoded = state.develop.defaults?.[control.key];
  return Number.isFinite(decoded) ? developClamp(decoded, control.min, control.max) : control.park;
}

function syncDevelopControls() {
  const { fields, ready } = state.develop;
  const touched = Object.keys(fields).length > 0;
  DEVELOP_CONTROLS.forEach(control => {
    const set = control.key in fields;
    const value = set ? fields[control.key] : developPark(control);
    const range = developNode(`develop-${control.key}`);
    const field = developNode(`develop-${control.key}-value`);
    if (range !== document.activeElement) range.value = String(value);  // never move a handle under the pointer
    if (field !== document.activeElement) field.value = set ? value.toFixed(control.decimals) : '';
    // The parked handle is a starting point, not this file's value: say so, because the empty field is only visual.
    range.setAttribute('aria-valuetext', set ? `${value.toFixed(control.decimals)}${control.unit}` : 'not set');
    if (set) range.removeAttribute('data-unset');
    else range.setAttribute('data-unset', '');
    range.disabled = field.disabled = !ready;
    developNode(`develop-${control.key}-reset`).disabled = !ready || !set;
  });
  CURVE_IDENTITY.forEach((_point, index) => developNode(`develop-point-${index}`).setAttribute('tabindex', ready ? '0' : '-1'));
  const lens = developNode('develop-lens_correction');
  lens.checked = fields.lens_correction === true;
  // A frame whose file carries no profile gets the box disabled rather than hidden, so the
  // panel's shape does not change from frame to frame.
  const supported = state.develop.lensCorrection !== false;
  lens.disabled = !ready || !supported;
  developNode('develop-lens-note').textContent = ready && !supported
    ? 'This file carries no lens profile Core Image can correct against.'
    : 'Uses the profile the camera recorded with the frame.';
  syncNoiseMode();
  developNode('develop-ask').disabled = state.busy || !ready || !roleReady('review') || aiRunning(`develop:${state.develop.photoId}`);
  developNode('develop-feedback').disabled = !ready;
  developNode('develop-discard-advice').disabled = state.busy || state.develop.beforeAdvice === null;
  developNode('develop-use-advice').hidden = !ready || !state.develop.advice || state.develop.beforeAdvice !== null;  // a parked suggestion needs no second way in
  developNode('develop-reset-all').disabled = !ready || (!touched && state.develop.saved === '{}');
  developNode('develop-apply-group').disabled = state.busy || !ready || !touched;
}

// The frame's saved settings and its offline flag arrive with its details; every render needs the raw itself.
function fillDevelop(photo) {
  const detail = state.details?.photo || {};
  const saved = state.details?.develop || {};
  const develop = state.develop;
  develop.controller?.abort();
  Object.assign(develop, {
    photoId: photo.id, fields: { ...saved, ...(saved.curve ? { curve: saved.curve.map(point => [...point]) } : {}) },
    neutral: null, defaults: {}, lensCorrection: true, zoomShown: false, histogram: {}, drag: null, controller: null,
    beforeAdvice: null, advice: state.details?.advice || null,
  });
  aiTaskLine(`develop:${photo.id}`, developNode('develop-task'));
  developNode('develop-advice').textContent = develop.advice?.rationale ?? '';  // the last stored suggestion, not yet parked
  developNode('develop-feedback').value = '';
  develop.saved = JSON.stringify(develop.fields);
  const offline = detail.offline === true;
  const raw = String(detail.format || '').toUpperCase() === 'ARW';
  develop.ready = !offline && raw;
  const reason = offline ? 'The original is offline. Plug its drive back in and reopen this frame: every develop render reads the raw.'
    : raw ? '' : 'Develop reads raw files; this frame is not one.';
  developNode('develop-tab').setAttribute('aria-disabled', String(!develop.ready));
  const message = developNode('develop-unavailable');
  message.textContent = reason;
  message.hidden = develop.ready;
  developNode('develop-workspace').hidden = !develop.ready;
  developNode('develop-status').textContent = '';  // the reason above says it once
  drawDevelopWidget();
  syncDevelopControls();
  if (develop.ready && developNode('develop-tab').getAttribute('aria-selected') === 'true') renderDevelopPreview();
}

// One gesture, one render: the whole frame, uncropped, with the newer request cancelling the older.
async function renderDevelopPreview() {
  const develop = state.develop;
  const photo = photoById(state.selectedId);
  if (!photo || !develop.ready || develop.photoId !== photo.id) return;
  develop.controller?.abort();
  const controller = new AbortController();
  develop.controller = controller;
  // Zoomed, ask for the raw's own size: sharpening and noise reduction act on single pixels,
  // so a render scaled to the viewport cannot show them however carefully it was decoded.
  const zoomed = developZoomed();
  const edge = zoomed ? 0 : Math.max(640, Math.round(Math.max(loupeViewport.clientWidth || 0, loupeViewport.clientHeight || 0)));
  const status = developNode('develop-status');
  status.textContent = zoomed ? 'Rendering this frame from the raw at full size…' : 'Rendering this frame from the raw…';
  let result;
  try {  // only the render is a render failure; a bug in the drawing below must not be reported as one
    result = await request(`/api/photos/${photo.id}/develop/preview`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
      body: JSON.stringify({ settings: develop.fields, long_edge: edge }),
    });
  } catch (error) {
    if (controller.signal.aborted) return;
    status.textContent = `${error.message}. The sliders still work; the render will be retried on your next change.`;
    return announce(status.textContent);
  }
  if (controller.signal.aborted || develop.photoId !== photo.id) return;
  develop.neutral = { temperature: result.neutral_temperature, tint: result.neutral_tint };
  develop.defaults = result.defaults && typeof result.defaults === 'object' ? result.defaults : {};
  develop.lensCorrection = result.lens_correction !== false;
  develop.histogram = result.histogram && !Array.isArray(result.histogram) ? result.histogram : {};
  drawDevelopWidget();
  syncDevelopControls();
  showPreview(loupeViewport, photo, result.image, loaded => {
    drawCropOverlay(shownCropRect());
    if (!zoomed) return;
    fullButton.textContent = `Full size, ${loaded.naturalWidth} by ${loaded.naturalHeight}`;
    // Hold the middle of the frame rather than the corner, as the embedded-JPEG path does.
    loupeViewport.scrollLeft = (loupeViewport.scrollWidth - loupeViewport.clientWidth) / 2;
    loupeViewport.scrollTop = (loupeViewport.scrollHeight - loupeViewport.clientHeight) / 2;
  }, !zoomed);
  develop.zoomShown = zoomed;
  fullButton.disabled = false;  // this tab renders its own full size, so an unreadable embedded JPEG is no bar
  if (!zoomed) {
    fullButton.textContent = 'Full size';  // the viewport now holds the develop render, not the full-size JPEG
    fullButton.setAttribute('aria-pressed', 'false');
  }
  const touched = Object.keys(develop.fields).length;
  status.textContent = touched ? `${touched} setting${touched === 1 ? '' : 's'} applied to this render. Nothing is saved until you leave the frame or export.` : 'The frame as the camera recorded it.';
}

// Saved on leaving the frame and before any export, so the stored settings are what the exported JPEG bakes in.
// An empty settings object is how the server is told to clear the frame.
async function saveDevelop() {
  const develop = state.develop;
  const photoId = develop.photoId;
  const body = JSON.stringify(develop.fields);
  if (!develop.ready || photoId === null || body === develop.saved) return;
  develop.saved = body;  // one save per change, even if the panel closes twice
  try {
    const result = await post(`/api/photos/${photoId}/develop`, { settings: JSON.parse(body) });
    const photo = photoById(photoId);
    if (photo && result.preview_url) {
      photo.preview_url = result.preview_url;  // the save re-rendered the contact-sheet preview
      photo.preview_kind = result.develop ? 'developed' : 'refined';
      refreshFrame(photoId);
      if (state.composition?.photo_id === photoId) state.composition.preview_url = result.preview_url;
    }
    announce(result.develop ? 'Develop settings saved.' : 'Develop settings cleared.');
  } catch (error) {
    develop.saved = null;  // unknown server state: every later body differs, so the next close tries again
    const message = `${photoById(photoId)?.name || 'This frame'}: develop settings could not be saved: ${error.message}`;
    if (develop.photoId === photoId) developNode('develop-status').textContent = message;  // the panel may already show another frame
    announce(message);
  }
}

function setDevelopField(control, value) {
  if (value === null) delete state.develop.fields[control.key];
  else state.develop.fields[control.key] = value;
  syncDevelopControls();
  announce(value === null ? `${control.label} reset to this file's own value` : `${control.label} ${value.toFixed(control.decimals)}${control.unit}`);
  renderDevelopPreview();
}

const NR_KEYS = ['luminance_nr', 'color_nr'];

// Auto leaves both amounts unset, so the decoder keeps its own baseline. Off pins both to
// zero. Manual is any other combination. Derived rather than stored: the mode is a view of
// the two amounts, and storing it as well would let the two disagree.
function developNoiseMode() {
  const fields = state.develop.fields;
  if (NR_KEYS.every(key => !(key in fields))) return 'auto';
  if (NR_KEYS.every(key => fields[key] === 0)) return 'off';
  return 'manual';
}

function syncNoiseMode() {
  const mode = developNoiseMode();
  document.querySelectorAll('input[name="develop-nr-mode"]').forEach(radio => {
    radio.checked = radio.value === mode;
    radio.disabled = !state.develop.ready;
  });
  developNode('develop-nr-manual').hidden = mode !== 'manual';
}

function setNoiseMode(mode) {
  const fields = state.develop.fields;
  if (mode === 'auto') NR_KEYS.forEach(key => delete fields[key]);
  else if (mode === 'off') NR_KEYS.forEach(key => { fields[key] = 0; });
  // Manual starts from whatever the decoder reported for this file, so the first render
  // after switching looks like the one before it.
  else NR_KEYS.forEach(key => { fields[key] = developClamp(state.develop.defaults?.[key] ?? 0.5, 0, 1); });
  syncDevelopControls();
  announce(`Noise reduction ${mode}`);
  renderDevelopPreview();
}

document.querySelectorAll('input[name="develop-nr-mode"]').forEach(radio => {
  radio.addEventListener('change', () => { if (radio.checked) setNoiseMode(radio.value); });
});

// Lens correction is a flag, not an amount: present means on, absent means the file's own
// default, which Core Image leaves off.
const lensBox = developNode('develop-lens_correction');
lensBox.addEventListener('change', () => {
  if (lensBox.checked) state.develop.fields.lens_correction = true;
  else delete state.develop.fields.lens_correction;
  syncDevelopControls();
  announce(`Lens correction ${lensBox.checked ? 'on' : 'off'}`);
  renderDevelopPreview();
});

DEVELOP_CONTROLS.forEach(control => {
  const range = developNode(`develop-${control.key}`);
  const field = developNode(`develop-${control.key}-value`);
  const bounded = value => developClamp(value, control.min, control.max);
  range.addEventListener('change', () => setDevelopField(control, bounded(Number(range.value))));  // change, not input: one render per gesture
  field.addEventListener('change', () => {
    const typed = field.value.trim();
    setDevelopField(control, typed === '' || !Number.isFinite(Number(typed)) ? null : bounded(Number(typed)));
  });
  developNode(`develop-${control.key}-reset`).addEventListener('click', () => setDevelopField(control, null));
});

// Dragging a point keeps the x values strictly ascending, which is what the server validates.
function moveCurvePoint(index, x, y) {
  const points = developCurve().map(point => [...point]);
  const low = index === 0 ? 0 : points[index - 1][0] + CURVE_GAP;
  const high = index === points.length - 1 ? 1 : points[index + 1][0] - CURVE_GAP;
  points[index] = [developClamp(x, low, Math.max(low, high)), developClamp(y, 0, 1)];
  state.develop.fields.curve = points;
  drawDevelopWidget();
  syncDevelopControls();
}

const curveSvg = developNode('develop-curve');
function curvePointerAt(event) {
  const box = curveSvg.getBoundingClientRect();
  return [developClamp((event.clientX - box.left) / Math.max(1, box.width), 0, 1),
    developClamp(1 - (event.clientY - box.top) / Math.max(1, box.height), 0, 1)];
}
curveSvg.addEventListener('pointerdown', event => {
  if (event.button !== 0 || !state.develop.ready) return;
  event.preventDefault();
  const [x, y] = curvePointerAt(event);
  const distance = index => Math.hypot(developCurve()[index][0] - x, developCurve()[index][1] - y);
  state.develop.drag = developCurve().reduce((best, _point, index) => (distance(index) < distance(best) ? index : best), 0);
  curveSvg.setPointerCapture(event.pointerId);
  developNode(`develop-point-${state.develop.drag}`).focus({ preventScroll: true });
  moveCurvePoint(state.develop.drag, x, y);
});
curveSvg.addEventListener('pointermove', event => {
  if (state.develop.drag === null) return;
  moveCurvePoint(state.develop.drag, ...curvePointerAt(event));
});
curveSvg.addEventListener('pointerup', event => {  // the render waits for the release: one gesture, one render
  const index = state.develop.drag;
  if (index === null) return;
  state.develop.drag = null;
  moveCurvePoint(index, ...curvePointerAt(event));
  announce(`Tone curve ${curveText(index)}`);
  renderDevelopPreview();
});
curveSvg.addEventListener('pointercancel', () => { state.develop.drag = null; });
// The keyboard fallback: arrow keys nudge the focused point by 1%.
const CURVE_KEYS = { ArrowLeft: [-0.01, 0], ArrowRight: [0.01, 0], ArrowDown: [0, -0.01], ArrowUp: [0, 0.01] };
CURVE_IDENTITY.forEach((_point, index) => {
  developNode(`develop-point-${index}`).addEventListener('keydown', event => {
    const delta = CURVE_KEYS[event.key];
    if (!delta || !state.develop.ready) return;
    event.preventDefault();
    event.stopPropagation();  // a nudge is not a pan and not a culling key
    const [x, y] = developCurve()[index];
    moveCurvePoint(index, x + delta[0], y + delta[1]);
    announce(`Tone curve ${curveText(index)}`);
    renderDevelopPreview();
  });
});

// Toggling a channel only redraws: the render it describes is unchanged.
HISTOGRAM_CHANNELS.forEach(channel => {
  const box = developNode(`develop-channel-${channel}`);
  box.addEventListener('change', () => {
    shownChannels[channel] = box.checked;
    drawDevelopWidget();
    announce(`${channel.toUpperCase()} ${box.checked ? 'shown' : 'hidden'}`);
  });
});

developNode('develop-curve-reset').addEventListener('click', () => {
  if (!state.develop.fields.curve) return announce('The tone curve is already straight.');
  delete state.develop.fields.curve;
  drawDevelopWidget();
  syncDevelopControls();
  announce('Tone curve reset to a straight line; the sliders keep their values.');
  renderDevelopPreview();
});
developNode('develop-reset-all').addEventListener('click', async () => {
  state.develop.fields = {};
  state.develop.beforeAdvice = null;  // there is nothing left to go back to, and the advice no longer describes the handles
  developNode('develop-advice').textContent = '';
  syncDevelopControls();
  announce('Every develop setting reset; this frame goes back to its own rendering.');
  await saveDevelop();  // the clear also re-renders the contact-sheet preview
  renderDevelopPreview();
});
// Advisory, like every other local AI control: the sliders move so the change is visible in the
// preview, but nothing is stored until the photographer leaves the tab, and Discard undoes the lot.
async function askDevelopAdvice() {
  const develop = state.develop;
  const photoId = develop.photoId;
  if (!develop.ready || photoId === null || !roleReady('review')) return;
  const before = JSON.stringify(develop.fields);
  const feedback = developNode('develop-feedback').value.trim();
  developNode('develop-ask').disabled = true;
  developNode('develop-status').textContent = 'Asking the loaded local vision model where to start…';
  try {
    const advice = await aiCall('Develop suggestion', () => post(`/api/photos/${photoId}/develop/suggest`,
      { settings: develop.fields, feedback: feedback || null }), `develop:${photoId}`);
    if (develop.photoId !== photoId) return;  // the server stored it; reopening the frame offers it again
    if (develop.beforeAdvice === null) develop.beforeAdvice = before;  // the first attempt is what Discard goes back to
    develop.advice = advice;
    develop.fields = advice.settings;
    developNode('develop-advice').textContent = advice.rationale;
    developNode('develop-feedback').value = '';
    developNode('develop-status').textContent = 'The handles are where the model would start. Nothing is saved yet.';
    syncDevelopControls();
    announce(advice.rationale);
    renderDevelopPreview();
  } catch (error) {
    if (develop.photoId !== photoId) return;
    developNode('develop-status').textContent = `Local AI advice is unavailable: ${error.message}. The handles are untouched.`;
    announce(developNode('develop-status').textContent);
  } finally {
    syncDevelopControls();  // re-enables Ask for whichever frame the panel now holds
  }
}

// The stored suggestion from an earlier visit, parked exactly as a fresh one would be.
function useStoredDevelopAdvice() {
  const develop = state.develop;
  if (!develop.ready || !develop.advice) return;
  if (develop.beforeAdvice === null) develop.beforeAdvice = JSON.stringify(develop.fields);
  develop.fields = JSON.parse(JSON.stringify(develop.advice.settings));
  developNode('develop-advice').textContent = develop.advice.rationale ?? '';
  developNode('develop-status').textContent = 'The handles are where the model would start. Nothing is saved yet.';
  syncDevelopControls();
  announce(developNode('develop-status').textContent);
  renderDevelopPreview();
}

function discardDevelopAdvice() {
  const develop = state.develop;
  if (develop.beforeAdvice === null) return;
  develop.fields = JSON.parse(develop.beforeAdvice);
  develop.beforeAdvice = null;
  developNode('develop-advice').textContent = '';
  developNode('develop-feedback').value = '';
  developNode('develop-status').textContent = 'Suggestion discarded; every handle is back where you left it.';
  syncDevelopControls();
  announce(developNode('develop-status').textContent);
  renderDevelopPreview();
}

developNode('develop-ask').addEventListener('click', () => askDevelopAdvice());
developNode('develop-discard-advice').addEventListener('click', () => discardDevelopAdvice());
developNode('develop-use-advice').addEventListener('click', () => useStoredDevelopAdvice());
developNode('develop-apply-group').addEventListener('click', async () => {
  const photoId = state.develop.photoId;
  state.busy = true;
  updateWorkflow();
  try {
    await saveDevelop();  // the job copies the stored settings, so this frame's must be stored first
    watchJob(await post(`/api/photos/${photoId}/develop/apply-to-group`));
  } catch (error) {
    document.querySelector('#develop-status').textContent = `Apply to this burst could not start: ${error.message}`;
    announce(document.querySelector('#develop-status').textContent);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
});
SIDES.forEach((side, index) => {
  const part = name => document.querySelector(`#compare-${side}-${name}`);
  const viewport = part('viewport');
  viewport.addEventListener('scroll', () => syncScroll(viewport, document.querySelector(`#compare-${SIDES[1 - index]}-viewport`)));
  viewport.addEventListener('keydown', event => panKey(event, viewport));
  viewport.addEventListener('focus', () => focusFrame(comparing[index], false));  // culling keys act on this pane
  const photo = () => photoById(comparing[index]);
  part('pick').addEventListener('click', () => decide(photo(), 'pick'));
  part('flag').addEventListener('click', () => decide(photo(), 'decision', 'flag'));
  part('reject').addEventListener('click', () => decide(photo(), 'decision', 'reject'));
  const rating = ratingSelect(part('rating'));
  rating.addEventListener('change', () => decide(photo(), 'rating', Number(rating.value)));
});
document.querySelector('#open-compare').addEventListener('click', openCompare);
document.querySelector('#swap-compare').addEventListener('click', () => {
  comparing.reverse();
  fillPanes();
  announce(`Swapped: ${photoById(comparing[0]).name} is on the left, ${photoById(comparing[1]).name} on the right`);
});
document.querySelector('#close-compare').addEventListener('click', closeView);
document.querySelector('#close-loupe').addEventListener('click', closeView);

function showExportResults(results, size) {
  const list = document.querySelector('#export-results');
  list.replaceChildren();
  results.forEach(result => {
    const item = document.createElement('li');
    item.textContent = result.success ? result.output : `Photo ${result.photo_id}: ${result.message}`;
    if (result.success) item.className = 'path';
    list.append(item);
  });
  const succeeded = results.filter(result => result.success).length;
  document.querySelector('#export-status').textContent = `${size} export complete: ${succeeded} saved, ${results.length - succeeded} failed.${succeeded < results.length ? ' Successful JPEGs remain; fix the reported problem and retry the failed photo.' : ''}`;
}

// Export tab and "Export this frame" share one path; a current-frame result is mirrored into the frame view.
async function runExport(scope) {
  await saveDevelop();  // export bakes in the stored settings, so the panel's work must be stored first
  const preset = document.querySelector('input[name="export-preset"]:checked').value;
  const payload = { scope, profile: state.profile, preset };
  if (keepOriginal.checked) payload.keep_original = true;
  if (!datedFolder.checked) payload.dated_folder = false;
  const watermark = document.querySelector('#export-watermark').value.trim();
  if (watermark) payload.watermark = watermark;
  if (scope === 'current') payload.photo_id = state.selectedId;
  // The filtered scope sends the contact sheet's URL query fields; the server validates them as /api/groups does.
  if (scope === 'filtered') Object.assign(payload, Object.fromEntries(new URLSearchParams(location.search)));
  const what = { current: 'current photo', keepers: 'all keepers', filtered: 'the filtered frames' }[scope];
  state.busy = true;
  updateWorkflow();
  const size = { share: 'Share size', web: 'Web size' }[preset] || 'Full resolution';
  document.querySelector('#export-status').textContent = `Exporting ${what} at ${size}… Keep this page open until the result appears.`;
  document.querySelector('#export-results').replaceChildren();
  try {
    if (scope !== 'current') {
      watchJob(await post('/api/export', payload));
      document.querySelector('#export-status').textContent = `Exporting ${what} at ${size}. Progress is in the dialog and the status bar; finished JPEGs are kept if you cancel.`;
    } else {
      showExportResults((await post('/api/export', payload)).results, size);
      announce(document.querySelector('#export-status').textContent);  // the frame view may be on a tab without #compose-status
    }
  } catch (error) {
    if (error.payload?.results) showExportResults(error.payload.results, size);
    else document.querySelector('#export-status').textContent = `${error.message}. Check darktable and the selected XMP, then retry. You can also use camera defaults.`;
    announce(document.querySelector('#export-status').textContent);
  } finally {
    state.busy = false;
    updateWorkflow();
    if (scope === 'current') document.querySelector('#compose-status').textContent = document.querySelector('#export-status').textContent;
    keepFrameViewFocus();
  }
}
const exportCurrentFrame = () => runExport('current');
// The frame dialog carries its own copy, because its export buttons honour it and the export panel is not on screen.
const keepOriginal = document.querySelector('#export-keep-original');
const loupeKeepOriginal = document.querySelector('#loupe-keep-original');
const datedFolder = document.querySelector('#export-dated-folder');
const loupeDatedFolder = document.querySelector('#loupe-dated-folder');
for (const [box, other] of [[keepOriginal, loupeKeepOriginal], [loupeKeepOriginal, keepOriginal]]) {
  box.addEventListener('change', () => {
    other.checked = box.checked;
    announce(box.checked ? 'Developed frames will also export their untouched version.' : 'Only the developed version will be exported.');
  });
}
for (const [box, other] of [[datedFolder, loupeDatedFolder], [loupeDatedFolder, datedFolder]]) {
  box.addEventListener('change', () => {
    other.checked = box.checked;
    announce(box.checked ? 'Exports go to a subfolder named for the date each frame was taken.' : 'Exports go straight into the JPEG folder.');
  });
}
document.querySelector('#export-jpegs').addEventListener('click', () => runExport(document.querySelector('input[name="export-scope"]:checked').value));

// The filtered scope sends the same fields the contact sheet's URL query holds; the server validates them alike.
function refinePayload() {
  const scope = document.querySelector('#refine-scope').value;
  if (scope === 'group') return { scope, group_id: state.selectedGroupId };
  return scope === 'filtered' ? { scope, ...Object.fromEntries(new URLSearchParams(location.search)) } : { scope };
}

document.querySelector('#refine-scope').addEventListener('change', updateWorkflow);
document.querySelector('#refine-previews').addEventListener('click', async () => {
  state.busy = true;
  updateWorkflow();
  const status = document.querySelector('#refine-status');
  status.textContent = '';
  try {
    watchJob(await post('/api/previews/refine', refinePayload()));
  } catch (error) {
    status.textContent = `Refine previews could not start: ${error.message}.`;
    announce(status.textContent);
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

async function loadCleared() {
  const count = document.querySelector('#cleared-count');
  try {
    const { frames, batches } = await request('/api/library/cleared');
    state.clearedFrames = frames;
    count.textContent = frames ? `${frameCount(frames)} in ${batches} batch${batches === 1 ? '' : 'es'}` : 'None';
  } catch (error) {
    state.clearedFrames = 0;
    count.textContent = `Could not count previously cleared frames: ${error.message}`;
  }
  updateWorkflow();
}
document.querySelector('#remove-cleared').addEventListener('click', () => cleanup('/api/library/remove-cleared', {}).then(loadCleared));

async function mergeBatches() {
  const groupIds = state.groups.map(group => group.id).filter(id => state.mergeSelection.has(id));
  state.busy = true;
  updateWorkflow();
  try {
    const result = await post('/api/groups/merge', { group_ids: groupIds });
    state.mergeSelection.clear();
    state.selectedId = null;
    state.selectedGroupId = result.group_id;
    await load();
    const index = state.groups.findIndex(group => group.id === result.group_id);
    const first = state.groups[index]?.photos[0];
    if (first) await inspect(first);
    scrollToGroup(result.group_id);
    announce(`Merged ${groupIds.length} batches into ${index < 0 ? 'one batch' : batchText(state.groups[index], index)}.`);
  } catch (error) {
    announce(`Could not merge batches: ${error.message}`);
  } finally {
    state.busy = false;
    updateWorkflow();
  }
}
document.querySelector('#merge-batches').addEventListener('click', mergeBatches);
// M merges from anywhere outside a text field or dialog; the button's disabled state is the only gate.
document.addEventListener('keydown', event => {
  if (event.key?.toLowerCase() !== 'm' || event.metaKey || event.ctrlKey || event.altKey || openView) return;
  const tag = event.target?.tagName;  // the rail's checkboxes are inputs too; only typing fields keep the key
  if (tag === 'TEXTAREA' || tag === 'SELECT' || (tag === 'INPUT' && !['checkbox', 'radio'].includes(event.target.type))) return;
  const button = document.querySelector('#merge-batches');
  if (button.disabled) return;
  event.preventDefault();
  return mergeBatches();
});
document.querySelector('#clear-workspace').addEventListener('click', () => cleanup('/api/library/clear', { confirmation: document.querySelector('#clear-confirmation').value }));

const watermarkInput = document.querySelector('#export-watermark');
function rememberWatermark() {
  try {
    if (watermarkInput.value.trim()) localStorage.setItem('lumen-watermark', watermarkInput.value);
    else localStorage.removeItem('lumen-watermark');
  } catch {
    document.querySelector('#export-status').textContent = 'Browser storage is unavailable; type the watermark again next visit.';
  }
}
watermarkInput.addEventListener('input', rememberWatermark);
watermarkInput.addEventListener('change', rememberWatermark);
const exportPresetRadios = [...document.querySelectorAll('input[name="export-preset"]')];
exportPresetRadios.forEach(radio => radio.addEventListener('change', () => {
  try {
    localStorage.setItem('lumen-export-preset', radio.value);
  } catch {
    // Storage is optional; the preset falls back to Full resolution next visit.
  }
}));
try {
  watermarkInput.value = localStorage.getItem('lumen-watermark') || '';
  const preset = exportPresetRadios.find(radio => radio.value === localStorage.getItem('lumen-export-preset'));
  if (preset) exportPresetRadios.forEach(radio => { radio.checked = radio === preset; });
} catch {
  // Storage is optional; the fields keep their defaults.
}
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

// Daily digest: facts always, narrative only when the review model answers; an existing file is replaced only after Confirm.
const digestButton = document.querySelector('#write-digest');
const digestConfirm = document.querySelector('#digest-confirm');
async function writeDigest(day, overwrite = false) {
  state.busy = true;  // The server holds the library lock for the whole model call, so culling waits visibly.
  updateWorkflow();
  try {
    const result = await aiCall(`Digest for ${day}`, () => post('/api/digests', overwrite ? { date: day, overwrite: true } : { date: day }));
    document.querySelector('#digest-result').textContent = result.file;
    announce(`Digest written for ${result.date}: ${result.file} (${result.narrative ? 'narrative included' : 'facts only'})`);
  } catch (error) {
    if (!error.payload?.exists) return announce(`Could not write the digest: ${error.message}`);
    digestConfirm.dataset.date = day;  // the day asked about at open time
    document.querySelector('#digest-confirm-text').textContent = `A digest for ${day} already exists. Overwrite it?`;
    if (digestConfirm.showModal) digestConfirm.showModal();
    else digestConfirm.hidden = false;
  } finally {
    state.busy = false;
    updateWorkflow();
  }
}
digestButton.addEventListener('click', () => writeDigest(new Date().toLocaleDateString('sv-SE')));
document.querySelector('#digest-confirm-yes').addEventListener('click', () => {
  if (digestConfirm.close) digestConfirm.close();
  else digestConfirm.hidden = true;
  return writeDigest(digestConfirm.dataset.date, true);
});
