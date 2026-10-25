const JOB_ACTIVE = ['queued', 'running'];
const CULLING_JOBS = ['import', 'ingest'];  // jobs that only add frames, so culling may continue alongside them
const state = {
  groups: [], photoCount: 0, selectedId: null, selectedGroupId: null, status: null, profile: null, acting: false, composition: null, critique: null, recommendedCropId: null, compositionRequest: 0, reviewRequest: 0,
  rejectSuggestions: [], job: null, jobTimer: null, jobRequest: 0, details: null, rejectedCount: 0, keeperCount: 0, shown: 200, focusedId: null, compare: [],
  astro: { job: null, folders: { darks: null, flats: null, biases: null }, lightsKey: null, triage: null },
  mergeSelection: new Set(), clearedFrames: 0,
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
async function aiCall(label, action) {
  aiStatus('running', `${label} running…`);
  try {
    const result = await action();
    aiStatus('done', `${label} finished`);
    return result;
  } catch (error) {
    aiStatus('failed', `${label} failed: ${error.message}`);
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
  document.querySelector('#ask-composition-ai').disabled = state.busy || !roleReady('review');
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
    const review = await aiCall('Composition review', () => post(`/api/photos/${photoId}/composition-review`));
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
  ask.className = 'secondary';
  ask.id = 'ask-burst-ai';
  ask.textContent = 'Ask local AI which frame is best';
  ask.disabled = !roleReady('review');
  ask.addEventListener('click', async () => {
    ask.disabled = true;
    state.busy = true;  // The server holds the library lock for the whole model call, so culling waits visibly.
    updateWorkflow();
    announce('Asking local AI to compare this burst');
    try {
      const data = await aiCall('Burst critique', () => post(`/api/groups/${group.id}/critique`));
      state.critique = { groupId: group.id, data };
      if (state.selectedId === photo.id) renderInspector(photo);
      announce(document.querySelector('#burst-advice')?.textContent || `Local AI suggests ${data.name}`);
    } catch (error) {
      announce(error.message);
      ask.disabled = !roleReady('review');
    } finally {
      state.busy = false;
      updateWorkflow();
    }
  });
  block.append(ask);
  if (roleUnavailable('review')) {
    const reason = document.createElement('p');
    reason.className = 'quiet';
    reason.id = 'burst-ai-availability';
    reason.textContent = roleUnavailable('review');
    ask.setAttribute('aria-describedby', reason.id);
    block.append(reason);
  }
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
    use.addEventListener('click', () => decide(suggested, 'pick', null, `.frame[data-id="${suggested.id}"]`));
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
  if (record?.latitude == null || record?.longitude == null) {
    line.textContent = place || 'No location in the file';  // a place set by hand needs no GPS fix
    return [line, ...placeEditor(photo, record)];
  }
  const degrees = (value, positive, negative) => `${Math.abs(value).toFixed(4)}° ${value < 0 ? negative : positive}`;
  line.textContent = `${place ? `${place} · ` : 'Location: '}${degrees(record.latitude, 'N', 'S')}, ${degrees(record.longitude, 'E', 'W')}${record.altitude == null ? '' : ` · ${Math.round(record.altitude)} m`}`;
  const link = document.createElement('a');
  link.href = `https://maps.apple.com/?ll=${record.latitude},${record.longitude}&q=${encodeURIComponent(photo.name)}`;
  link.target = '_blank';
  link.rel = 'noopener';
  link.textContent = 'Open in Apple Maps';
  return [line, link, ...placeEditor(photo, record)];
}

// The photographer names the place for this frame or its burst; remembering adds it to the local index at this GPS fix.
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
  const check = (id, text) => {
    const label = document.createElement('label');
    label.className = 'check';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.id = id;
    input.checked = true;
    label.append(input, ` ${text}`);
    form.append(label);
    return input;
  };
  const burst = check('place-burst', 'Apply to whole burst');
  const remember = check('place-remember', 'Remember for nearby frames');
  if (record?.latitude == null || record?.longitude == null) {
    remember.checked = false;
    remember.disabled = true;
    const reason = document.createElement('span');
    reason.id = 'place-no-fix';
    reason.className = 'quiet';
    reason.textContent = 'no GPS fix';
    remember.setAttribute('aria-describedby', reason.id);
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
    return savePlace(photo, { name: name.value, country: country.value, scope: burst.checked ? 'burst' : 'frame', remember: remember.checked });
  });
  if (state.placeFocus) { state.placeFocus = false; window.setTimeout(() => edit.focus(), 0); }
  return [edit, form];
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
  const heading = document.createElement('h3');
  heading.textContent = photo.name;
  const verdict = document.createElement('p');
  verdict.className = 'quiet';
  const group = state.groups.find(item => item.photos.includes(photo));
  verdict.textContent = group && allRejected(group) ? NO_KEEPER
    : photo.id === photo.groupPick ? 'Best measured frame in this group.' : 'Alternate frame; compare its component scores.';
  inspection.append(heading, verdict);
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
    choose.className = 'secondary';
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
  tag.textContent = 'Add local AI tags';
  tag.dataset.tagPhoto = 'true';
  tag.disabled = !roleReady('fast');
  tag.addEventListener('click', async () => {
    tag.disabled = true;
    state.busy = true;  // The server holds the library lock for the whole model call.
    updateWorkflow();
    try {
      await aiCall(`Tagging ${photo.name}`, () => request(`/api/photos/${photo.id}/tag`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }));
      announce(`Tags added to ${photo.name}`);
      await inspect(photo);
    } catch (error) {
      announce(error.message);
      tag.disabled = false;
    } finally {
      state.busy = false;
      updateWorkflow();
    }
  });
  actions.append(tag);
  inspection.append(actions);
  if (roleUnavailable('fast')) {
    const reason = document.createElement('p');
    reason.className = 'quiet';
    reason.id = 'tag-photo-availability';
    reason.textContent = roleUnavailable('fast');
    tag.setAttribute('aria-describedby', reason.id);
    inspection.append(reason);
  }
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
  block.append(heading, note);
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
  ask.className = 'secondary';
  ask.id = 'ask-caption-ai';
  ask.textContent = 'Write caption with local AI';
  ask.disabled = !roleReady('review');
  ask.addEventListener('click', async () => {
    ask.disabled = true;
    state.busy = true;  // The server holds the library lock for the whole model call.
    updateWorkflow();
    announce(`Asking local AI to caption ${photo.name}`);
    try {
      const drafted = await aiCall(`Caption for ${photo.name}`, () => post(`/api/photos/${photo.id}/caption`));
      caption.value = drafted.caption || '';
      alt.value = drafted.alt_text || '';
      Object.assign(data, { caption: drafted.caption, alt_text: drafted.alt_text });
      announce(`Local AI caption written for ${photo.name}; edit and save to change it`);
    } catch (error) {
      announce(error.message);
    } finally {
      ask.disabled = !roleReady('review');
      state.busy = false;
      updateWorkflow();
    }
  });
  actions.append(save, ask);
  block.append(actions);
  if (roleUnavailable('review')) {
    const reason = document.createElement('p');
    reason.className = 'quiet';
    reason.id = 'caption-ai-availability';
    reason.textContent = roleUnavailable('review');
    ask.setAttribute('aria-describedby', reason.id);
    block.append(reason);
  }
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

const CULLING_KEYS = { p: 'pick', r: 'keepOnly', x: 'reject', f: 'flag', u: 'clear', c: 'compare', z: 'full', Enter: 'loupe', Escape: 'close' };
const CULLING_ACTIONS = {
  pick: photo => decide(photo, 'pick'),
  keepOnly,
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
  // Only frame cards cull on the sheet; in the frame view its own buttons cull too.
  if (target?.tagName === 'BUTTON' && target.className !== 'frame' && openView?.id !== 'loupe') return;
  if ((key === '[' || key === ']') && openView?.id === 'loupe') { event.preventDefault(); return stepLoupe(key === ']' ? 1 : -1); }
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
  if (!photos.length) {
    const empty = document.createElement('p');
    empty.className = 'quiet';
    empty.textContent = location.search ? 'No frames match these filters.' : 'Import a folder to build your first contact sheet.';
    sheet.append(empty);
  }
  document.querySelector('#photo-count').textContent = frameCount(photos.length);
  if (!state.groups.length) rail.textContent = location.search ? 'No batches match these filters.' : 'No imported batches yet.';
  updateWorkflow();
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
  date_from: '#filter-from', date_to: '#filter-to', camera: '#filter-camera', decision: '#filter-decision', min_rating: '#filter-rating',
  keepers_only: '#filter-keepers', tagged_only: '#filter-tagged', text: '#filter-text', show_rejected: '#show-rejected',
};
const FLAG_FILTERS = ['keepers_only', 'tagged_only', 'show_rejected'];
const FILTER_DEFAULTS = { decision: 'any', min_rating: '0' };
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

let resumed = false;
async function load() {
  const [status, groups] = await Promise.all([request('/api/status'), request(`/api/groups${location.search}`)]);
  state.status = status;
  state.groups = groups.groups;
  state.mergeSelection = new Set([...state.mergeSelection].filter(id => state.groups.some(group => group.id === id)));
  state.rejectedCount = groups.rejected_count || 0;
  state.photoCount = groups.photo_count;
  state.keeperCount = groups.keeper_count;
  syncFilters(groups.cameras);
  const selectedGroup = state.groups.find(group => group.photos.some(photo => photo.id === state.selectedId))
    || (state.selectedId === null ? state.groups.find(group => group.id === state.selectedGroupId) : undefined);
  state.selectedGroupId = selectedGroup?.id ?? null;
  if (!selectedGroup) {
    state.selectedId = null;
    inspection.textContent = 'Select a frame to see why it ranked where it did.';
  }
  // A server older than this page answers without omlx_roles; the sheet must still render, so the status line only reports it.
  const roleText = role => `${role} ${status.omlx_roles[role].model} ${status.omlx_roles[role].loaded ? 'loaded' : 'not loaded'}`;
  document.querySelector('#system-status').textContent = status.omlx_roles
    ? `${status.omlx_live.summary} · ${roleText('fast')} · ${roleText('review')}`
    : `${status.omlx_live?.summary || 'Status unavailable'} · the running Lumen server is older than this page; restart lumen serve to enable AI tools`;
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

// Import lives in a dialog opened from the tab row, so the sheet keeps the space.
const importDialog = document.querySelector('#import-dialog');
function showImport(open) {
  if (open) {
    if (importDialog.showModal) importDialog.showModal(); else importDialog.hidden = false;
    document.querySelector('#import-path').focus();
  } else if (importDialog.close) importDialog.close(); else importDialog.hidden = true;
}
document.querySelector('#open-import').addEventListener('click', () => showImport(true));
document.querySelector('#close-import').addEventListener('click', () => showImport(false));
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
    showImport(false);
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

const JOB_NAMES = { import: 'Import', export: 'Export', tag: 'Tagging', assist: 'Local AI assist', review_rejects: 'Reject review', triage: 'Sky triage', astro: 'Stack', ingest: 'Ingest', refine_previews: 'Refine previews' };

function jobText(job) {
  const name = JOB_NAMES[job.kind] || 'Job';
  const result = job.result || {};
  const summary = job.kind === 'import' && job.result ? ` ${result.imported} new, ${result.updated} updated, ${result.skipped} skipped, ${result.failed} failed${result.previously_cleared ? `, ${result.previously_cleared} previously cleared` : ''}.`
    : job.kind === 'tag' && job.result ? ` ${result.tagged} tagged, ${result.skipped} skipped, ${result.failed} failed.`
    : job.kind === 'assist' && job.result ? ` ${result.tagged} tagged, ${result.reviewed} scanned (${result.suggested} suggested rejects), ${result.captioned} captioned, ${result.skipped} already done, ${result.failed} failed.`
    : job.kind === 'triage' && job.result ? ` ${result.triaged} triaged, ${result.failed} failed.`
    : job.kind === 'review_rejects' && job.result ? ` ${result.reviewed} reviewed, ${result.suggested} suggested, ${result.skipped} skipped, ${result.failed} failed.`
    : job.kind === 'refine_previews' && job.result ? ` ${result.refined} refined, ${result.skipped} skipped, ${result.failed} failed.`
    : job.kind === 'export' && job.result ? ` ${result.exported} saved, ${result.failed} failed${result.skipped ? `, ${result.skipped} skipped` : ''}.` : '';
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
  if (job.kind === 'export') renderExportDialog(job);
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
document.querySelector('#cancel-job').addEventListener('click', cancelCurrentJob);

// A keepers export runs as a job; this modal shows its progress and offers Cancel, then the outcome and Close.
const exportDialog = document.querySelector('#export-dialog');
function renderExportDialog(job) {
  const active = JOB_ACTIVE.includes(job.status);
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
  if (!active) {
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
  document.querySelector('#rejected-count').textContent = `(${state.rejectedCount})`;
  document.querySelectorAll('[data-decision-control]').forEach(button => { button.disabled = state.cullingBlocked; });
  const rating = document.querySelector('#rating');
  if (rating) rating.disabled = state.cullingBlocked;
  document.querySelector('#group-cleanup-count').textContent = group ? `Burst ${groupIndex + 1} · ${frameCount(group.frame_count)}` : 'No batch selected · 0 frames';
  document.querySelector('#workspace-cleanup-count').textContent = `${state.groups.length} batch${state.groups.length === 1 ? '' : 'es'} · ${frameCount(state.photoCount)}`;
  document.querySelector('#clear-help').textContent = `Use the exact uppercase word CLEAR. This removes all ${frameCount(state.photoCount)} from Lumen, including photos outside visible batches.`;
  document.querySelector('#export-availability').textContent = available ? 'darktable is ready. Existing JPEGs are kept; each export gets a unique name.' : 'darktable-cli is unavailable. Install darktable or add it to PATH, restart Lumen, then reload this page.';
  const scope = document.querySelector('input[name="export-scope"]:checked').value;
  document.querySelector('#export-jpegs').disabled = state.busy || !available || (scope === 'current' ? !photo : !keepers);
  document.querySelector('#loupe-export').disabled = state.busy || !available || !photo;
  document.querySelector('#refine-availability').textContent = available ? '' : 'darktable-cli is unavailable. Install darktable or add it to PATH, restart Lumen, then reload this page.';
  document.querySelector('#refine-previews').disabled = state.busy || !available || (document.querySelector('#refine-scope').value === 'group' && !group);
  document.querySelector('#remove-group').disabled = state.busy || !group;
  document.querySelector('#remove-cleared').disabled = state.busy || !state.clearedFrames;
  document.querySelector('#merge-batches').disabled = state.busy || state.mergeSelection.size < 2;
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
  const fastLoaded = roleReady('fast');
  document.querySelector('#tag-all').disabled = state.busy || !fastLoaded;
  document.querySelector('#tag-all-availability').textContent = roleUnavailable('fast');
  document.querySelector('#tag-keepers').textContent = `Tag keepers with local AI (${keepers} keeper${keepers === 1 ? '' : 's'})`;
  document.querySelector('#tag-keepers').disabled = state.busy || !keepers || !fastLoaded;
  document.querySelector('#tag-keepers-availability').textContent = roleUnavailable('fast');
  updateExpandSearch();
  const bothLoaded = fastLoaded && roleReady('review');
  document.querySelector('#assist-keepers').textContent = `Run all local AI on keepers (${keepers} keeper${keepers === 1 ? '' : 's'})`;
  document.querySelector('#assist-keepers').disabled = state.busy || !keepers || !bothLoaded;
  document.querySelector('#assist-keepers-availability').textContent = roleUnavailable('fast') || roleUnavailable('review');
  document.querySelector('#scan-rejects').disabled = state.busy || !keepers || !fastLoaded;
  document.querySelector('#scan-rejects-availability').textContent = roleUnavailable('fast');
  document.querySelector('#reject-all-shown').disabled = state.cullingBlocked || !state.rejectSuggestions.length;
  document.querySelector('#composition-availability').textContent = roleUnavailable('review');
  document.querySelector('#retag-all').disabled = state.busy;
  document.querySelector('#redo-keepers').disabled = state.busy;
  document.querySelector('#ask-composition-ai').disabled = state.busy || !roleReady('review') || !state.composition || state.composition.photo_id !== state.selectedId;
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
  const flagged = flaggedLights().length;
  document.querySelector('#triage-astro').disabled = state.busy || !group || !roleReady('fast');
  document.querySelector('#triage-astro-availability').textContent = roleUnavailable('fast');
  document.querySelector('#exclude-flagged').textContent = `Exclude flagged lights (${flagged})`;
  document.querySelector('#exclude-flagged').disabled = activeJob || !flagged;
  document.querySelector('#astro-confirm').disabled = activeJob;
  document.querySelectorAll('input[name="astro-recipe"]').forEach(input => { input.disabled = activeJob; });
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
  collapsePanel(false);  // choosing a tab always shows its panel
  document.querySelector('#main-content').scrollTo({ top: 0, behavior: scrollMotion() });  // the panel sits above the sheet
  try { localStorage.setItem('lumen-tab', tab.id); } catch { /* per-visit only */ }
  if (tab.id === 'review-tab') { loadRejectSuggestions(); loadVocabulary(); }
  if (tab.id === 'clear-tab') loadCleared();
}

function collapsePanel(collapsed) {
  document.querySelector('#workflow-panels').hidden = collapsed;
  document.querySelector('#collapse-panel').setAttribute('aria-pressed', String(collapsed));
  try {
    if (collapsed) localStorage.setItem('lumen-panel-collapsed', '1');
    else localStorage.removeItem('lumen-panel-collapsed');
  } catch { /* per-visit only */ }
}
document.querySelector('#collapse-panel').addEventListener('click', () => {
  const collapsed = document.querySelector('#collapse-panel').getAttribute('aria-pressed') !== 'true';
  collapsePanel(collapsed);
  announce(collapsed ? 'Workflow panel collapsed; the contact sheet has the space.' : 'Workflow panel shown.');
});
try {
  const rememberedTab = tabs.find(tab => tab.id === localStorage.getItem('lumen-tab'));
  if (rememberedTab) selectTab(rememberedTab);  // before load(), so the first load fills the right panel; a removed tab stays on Export
  if (localStorage.getItem('lumen-panel-collapsed') === '1') collapsePanel(true);
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
// Keep scroll-into-view clear of the sticky import bar and tab rail.
if (window.ResizeObserver) {
  const top = document.querySelector('.main-top');
  new ResizeObserver(() => document.documentElement.style.setProperty('--sticky-top', `${top.offsetHeight}px`)).observe(top);
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
document.querySelector('#show-rejected').addEventListener('change', () => applyFilters().catch(error => announce(error.message)));
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
  document.querySelector('#expand-search-availability').textContent = roleUnavailable('fast');
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
document.querySelector('#reset-filters').addEventListener('click', () => {
  cancelFind();
  return applyFilters('').catch(error => announce(error.message));
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
let syncing = null;  // the viewport whose scroll we just set; its echo is ignored

function showView(dialog) {
  openView = dialog;
  viewOrigin = state.focusedId;
  if (dialog.showModal) dialog.showModal();
  else dialog.hidden = false;
}

function closeView() {
  const dialog = openView;
  if (!dialog) return;
  openView = null;
  comparing = [];
  if (dialog.close) dialog.close();
  else dialog.hidden = true;
  focusFrame(viewOrigin);
}

// Scale 1 of the stored preview in both panes, so zoom matches; preview_url carries the version query.
function showPreview(viewport, photo, src = photo.preview_url, loaded = () => {}) {
  const image = document.createElement('img');
  image.alt = photo.name;
  image.addEventListener('load', () => { image.width = image.naturalWidth; image.height = image.naturalHeight; loaded(image); });
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
  document.querySelector('#loupe-heading').textContent = photo.name;
  showPreview(loupeViewport, photo);
  Object.assign(fullButton, { disabled: false, textContent: 'Full size' });
  fullButton.setAttribute('aria-pressed', 'false');
  inspect(photo).catch(error => announce(error.message));  // sets selectedId before its first await, so the crops below are this frame's
  loadComposition();
  const ids = Array.from(document.querySelectorAll('.frame'), card => Number(card.dataset.id));
  announce(`${photo.name}, ${ids.indexOf(photo.id) + 1} of ${ids.length}`);
}

// Swap between the stored preview and the camera's embedded full-size JPEG; the button names the real pixel size.
function toggleFullSize() {
  const photo = photoById(state.selectedId);
  if (!photo || fullButton.disabled) return;
  if (fullButton.getAttribute('aria-pressed') === 'true') {
    showPreview(loupeViewport, photo);
    fullButton.textContent = 'Full size';
    fullButton.setAttribute('aria-pressed', 'false');
    return announce('Preview size');
  }
  fullButton.setAttribute('aria-pressed', 'true');  // a second z while it loads returns to the preview
  const image = showPreview(loupeViewport, photo, `/full/${photo.id}?${photo.preview_url.split('?')[1] || ''}`, loaded => {
    if (loupeViewport.children[0] !== image) return;  // stepped on or toggled off while it loaded
    fullButton.textContent = `Full size, ${loaded.naturalWidth} by ${loaded.naturalHeight}`;
    loupeViewport.scrollLeft = (loupeViewport.scrollWidth - loupeViewport.clientWidth) / 2;
    loupeViewport.scrollTop = (loupeViewport.scrollHeight - loupeViewport.clientHeight) / 2;
    announce(fullButton.textContent);
  });
  image.addEventListener('error', async () => {
    if (loupeViewport.children[0] !== image) return;
    const preview = showPreview(loupeViewport, photo);
    fullButton.disabled = true;
    fullButton.setAttribute('aria-pressed', 'false');
    keepFrameViewFocus();
    const offline = await fetch(image.src).then(response => response.status === 503, () => false);  // the img error carries no status
    if (loupeViewport.children[0] !== preview) return;  // stepped on while asking
    fullButton.textContent = offline ? 'original is offline' : 'no full-size image in this file';
    announce(fullButton.textContent);
  });
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
loupeViewport.addEventListener('keydown', event => panKey(event, loupeViewport));
fullButton.addEventListener('click', toggleFullSize);
document.querySelector('#loupe-export').addEventListener('click', () => exportCurrentFrame());
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
  const preset = document.querySelector('input[name="export-preset"]:checked').value;
  const payload = { scope, profile: state.profile, preset };
  const watermark = document.querySelector('#export-watermark').value.trim();
  if (watermark) payload.watermark = watermark;
  if (scope === 'current') payload.photo_id = state.selectedId;
  state.busy = true;
  updateWorkflow();
  const size = { share: 'Share size', web: 'Web size' }[preset] || 'Full resolution';
  document.querySelector('#export-status').textContent = `Exporting ${scope === 'current' ? 'current photo' : 'all keepers'} at ${size}… Keep this page open until the result appears.`;
  document.querySelector('#export-results').replaceChildren();
  try {
    if (scope === 'keepers') {
      watchJob(await post('/api/export', payload));
      document.querySelector('#export-status').textContent = `Exporting all keepers at ${size}. Progress is in the dialog and the status bar; finished JPEGs are kept if you cancel.`;
    } else showExportResults((await post('/api/export', payload)).results, size);
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
