const state = { videoId: null, collectionId: null, collectionName: '', videos: [], rejectionReasons: [], analysisAudio: { mode:'split', single_track:1, microphone_track:2, all_sounds_track:1, game_track:3, use_all_sounds:true, use_game:true }, discovery: { active_profile:'general', profiles:[] }, resultMode: 'all', activeResults: null, previewAudio: null, editingSegment: null, clipEditorOpen: true, captionPositions: {}, exportNames: {}, globalCaption: { captions_preset: 'highlight', base_color: '#FFFFFF', active_color: '#FFFF00' }, globalExport: { layout: 'original', audio_track: 1 }, captionDirty: false, exportDirty: false, analysisAudioDirty: false, statusErrorUntil: 0 };
const $ = (selector) => document.querySelector(selector);
const fmt = (seconds) => new Date(seconds * 1000).toISOString().slice(11, 19);
const clamp = (number) => Math.max(0, Math.min(100, Number(number || 0)));
const bytes = (value) => { const amount = Number(value || 0); if (!amount) return '0 B'; const units = ['B', 'KB', 'MB', 'GB', 'TB']; const index = Math.min(units.length - 1, Math.floor(Math.log(amount) / Math.log(1024))); return `${(amount / (1024 ** index)).toFixed(index < 2 ? 0 : 1)} ${units[index]}`; };

document.addEventListener('pointerdown', (event) => {
  const button = event.target.closest('button, .quiet');
  if (!button || button.disabled) return;
  button.classList.remove('button-pop');
  requestAnimationFrame(() => button.classList.add('button-pop'));
  window.setTimeout(() => button.classList.remove('button-pop'), 320);
  if (button.closest('.actions')) {
    button.classList.remove('action-clicked');
    void button.offsetWidth;
    button.classList.add('action-clicked');
    window.setTimeout(() => button.classList.remove('action-clicked'), 460);
  }
});

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, { cache: 'no-store', ...options });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || 'Request failed.');
  const type = response.headers.get('content-type') || '';
  return type.includes('application/json') ? response.json() : response;
}
function uploadVideo(file) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const data = new FormData();
    data.append('file', file);
    request.open('POST', '/api/videos');
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) setUploadProgress(event.loaded / event.total * 100, `Uploading ${file.name}: ${Math.round(event.loaded / event.total * 100)}%`);
      else setUploadProgress(0, `Uploading ${file.name}...`);
    };
    request.onerror = () => reject(new Error('The browser could not reach the local upload endpoint.'));
    request.onload = () => {
      let body = {};
      try { body = JSON.parse(request.responseText || '{}'); } catch { /* handled below */ }
      if (request.status >= 200 && request.status < 300) resolve(body);
      else reject(new Error(body.detail || `Upload failed (HTTP ${request.status}).`));
    };
    request.send(data);
  });
}
function message(text, error = false) { if (error) state.statusErrorUntil = Date.now() + 12000; const el = $('#status'); el.textContent = text; el.style.color = error ? 'var(--danger)' : 'var(--muted)'; }
function setUploadProgress(percent, label, error = false) {
  const block = $('#upload-progress'); block.hidden = false; block.classList.toggle('error', error);
  $('#upload-progress-label').textContent = label;
  $('#upload-progress-fill').style.width = `${clamp(percent)}%`;
}
function make(tag, className, text = '') { const el = document.createElement(tag); if (className) el.className = className; if (text) el.textContent = text; return el; }
function updateSelectionSummary() {
  const prompt = $('#active-prompt').value.trim(); const collection = state.collectionName || 'none';
  $('#selection-summary').textContent = `Active collection: ${collection}. Active prompt: ${prompt ? prompt : 'none'}.`;
  $('#similar-button').disabled = !state.collectionId;
  $('#import-folder-button').disabled = !state.collectionId;
  $('#generate-prompt-button').disabled = !state.collectionId;
}
function openSegmentInRecording(segment) {
  const dialog = $('#video-dialog'); const player = $('#full-video');
  $('#dialog-title').textContent = `Full recording: ${fmt(segment.start_seconds)} - ${fmt(segment.end_seconds)}`;
  $('#dialog-transcript').textContent = segment.transcript || '';
  player.src = `/api/videos/${segment.video_id}/stream#t=${segment.start_seconds.toFixed(2)},${segment.end_seconds.toFixed(2)}`;
  player.onloadedmetadata = () => { player.currentTime = segment.start_seconds; };
  if (!dialog.open) dialog.showModal();
}

function setClipEditorOpen(open) {
  state.clipEditorOpen = open;
  document.body.classList.toggle('clip-editor-closed', !open);
  $('#clip-editor-sidebar').setAttribute('aria-hidden', String(!open));
  $('#clip-editor-toggle').hidden = open;
  $('#clip-editor-toggle').setAttribute('aria-expanded', String(open));
  try { localStorage.setItem('clipfinder-clip-editor-open', open ? 'true' : 'false'); } catch { /* Optional preference only. */ }
}

async function loadVideos() {
  const [videos, storage] = await Promise.all([api('/videos'), api('/storage')]); state.videos = videos; const box = $('#videos'); box.replaceChildren();
  $('#storage-summary').textContent = `Source recordings: ${bytes(storage.video_bytes)} (${storage.video_count}) / exported clips: ${bytes(storage.clip_bytes)} (${storage.clip_count})`;
  if (!videos.length) { box.append(make('p', 'hint', 'No recordings yet.')); return; }
  for (const video of videos) {
    const card = make('article', `video ${video.status} ${state.videoId === video.id ? 'selected' : ''}`);
    const info = make('div', 'video-info'); info.append(make('strong', 'video-name', video.original_name));
    info.append(make('p', 'video-meta', `${video.duration_seconds ? fmt(video.duration_seconds) : '--:--:--'} / ${bytes(video.size_bytes)} / ${video.message || video.status}`));
    const progress = make('div', 'video-progress'); const track = make('div', 'progress-track'); const fill = make('div', 'progress-fill'); fill.style.width = `${clamp(video.progress)}%`; track.append(fill); progress.append(track, make('strong', '', `${clamp(video.progress)}%`));
    card.append(info, make('span', 'pill', video.status), progress);
    if (['failed', 'interrupted', 'ready'].includes(video.status)) { const label = video.status === 'ready' ? 'Reanalyze recording' : 'Run analysis again'; const retry = make('button', 'quiet', label); retry.onclick = async (event) => { event.stopPropagation(); await api(`/videos/${video.id}/analyse`, { method:'POST' }); message('Analysis queued again.'); await refreshDashboard(); }; const remove = make('button', 'quiet danger-button', 'Delete recording'); remove.onclick = async (event) => { event.stopPropagation(); if (!window.confirm(`Delete ${video.original_name}, its analysis data and ${bytes(video.size_bytes)} of source video? Exported clips will be kept.`)) return; remove.disabled = true; try { await api(`/videos/${video.id}`, { method:'DELETE' }); if (state.videoId === video.id) { state.videoId = null; state.activeResults = null; $('#workspace').hidden = true; } message('Source recording and its analysis data deleted. Exported clips were kept.'); await refreshDashboard(); } catch (error) { message(error.message, true); remove.disabled = false; } }; card.append(retry, remove); }
    card.onclick = () => selectVideo(video); box.append(card);
  }
}

async function selectVideo(video) {
  state.videoId = video.id; state.resultMode = 'all'; state.activeResults = null; state.captionPositions = {}; state.exportNames = {}; clearClipEditor(); $('#workspace').hidden = false; $('#selected-title').textContent = `Candidates: ${video.original_name}`;
  updateSelectionSummary(); await Promise.all([loadVideos(), loadSegments()]);
}

async function legacyLoadSegments(custom = null) {
  if (!state.videoId) return;
  const selectedTag = $('#tag-search').value; const selectedRating = $('#rating-search').value; const hideReading = $('#hide-reading').checked && selectedTag !== 'reading';
  const values = custom
    ? custom.filter((segment) => (!selectedTag || (segment.tags || []).includes(selectedTag)) && (!selectedRating || segment.rating === selectedRating) && (!hideReading || !(segment.tags || []).includes('reading')))
    : await api(`/videos/${state.videoId}/segments?q=${encodeURIComponent($('#search').value)}&tag=${encodeURIComponent(selectedTag)}&rating=${encodeURIComponent(selectedRating)}&hide_reading=${hideReading}&show_duplicates=${$('#show-duplicates').checked}`);
  const box = $('#segments'); box.replaceChildren();
  if (!values.length) { box.append(make('p', 'hint', 'No candidates yet. Analysis may still be running.')); return; }
  const template = $('#segment-template');
  for (const segment of values) {
    const node = template.content.cloneNode(true); const article = node.querySelector('article'); article.onclick = null; article.classList.add(segment.rating);
    const score = segment.similarity !== undefined ? ` / prompt ${Math.round(segment.similarity * 100)}%` : '';
    node.querySelector('.time').textContent = `${fmt(segment.start_seconds)} - ${fmt(segment.end_seconds)}${score}`;
    node.querySelector('.ranking').textContent = segment.ranking_score ? `Suggested score ${segment.ranking_score}/99 - ${segment.ranking_reason}` : `Clip quality ${segment.quality_score || 0}/99${(segment.quality_signals || []).length ? ` - ${(segment.quality_signals || []).join(', ')}` : ''}`;
    const tags = node.querySelector('.tags');
    for (const tag of segment.tags || []) tags.append(make('span', 'tag', tag));
    node.querySelector('.transcript').textContent = segment.transcript || 'No recognized speech';
    const startInput = node.querySelector('[data-start]'); const endInput = node.querySelector('[data-end]'); const captionPosition = node.querySelector('[data-caption-position]'); const ratingSelect = node.querySelector('[data-rating-select]'); const reviewReason = node.querySelector('[data-review-reason]'); const transcriptInput = node.querySelector('[data-transcript]'); const censorToggle = node.querySelector('[data-censor-profanity]'); const exportName = node.querySelector('[data-export-name]');
    startInput.value = Number(segment.start_seconds).toFixed(1); endInput.value = Number(segment.end_seconds).toFixed(1);
    for (const reason of state.rejectionReasons) { if (![...reviewReason.options].some((option) => option.value === reason)) { const option = document.createElement('option'); option.value = reason; option.textContent = reason; reviewReason.append(option); } }
    captionPosition.value = state.captionPositions[segment.id] || 'bottom'; ratingSelect.value = segment.rating;
    if ([...reviewReason.options].some((option) => option.value === segment.review_reason)) reviewReason.value = segment.review_reason;
    captionPosition.onchange = () => { state.captionPositions[segment.id] = captionPosition.value; };
    transcriptInput.value = segment.transcript || '';
    censorToggle.checked = Boolean(segment.censor_profanity);
    censorToggle.onchange = async () => {
      censorToggle.disabled = true;
      try {
        const updated = await api(`/segments/${segment.id}/censor`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({censor_profanity:censorToggle.checked}) });
        segment.censor_profanity = updated.censor_profanity;
        message(updated.censor_profanity ? 'Profanity censoring enabled for this clip export.' : 'Profanity censoring disabled for this clip export.');
      } catch (error) { censorToggle.checked = !censorToggle.checked; message(error.message, true); } finally { censorToggle.disabled = false; }
    };
    exportName.value = state.exportNames[segment.id] || '';
    exportName.oninput = () => { state.exportNames[segment.id] = exportName.value; };
    node.querySelector('[data-open]').onclick = () => openSegmentInRecording(segment);
    const saveRange = node.querySelector('[data-save-range]'); saveRange.onclick = async () => {
      const start_seconds = Number(startInput.value); const end_seconds = Number(endInput.value);
      if (!Number.isFinite(start_seconds) || !Number.isFinite(end_seconds)) return message('Enter valid start and end times.', true);
      saveRange.disabled = true; saveRange.textContent = 'Updating captions...';
      try {
        await api(`/segments/${segment.id}/timing`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({start_seconds, end_seconds}) });
        message('Clip range and captions updated.'); state.resultMode = 'all'; await loadSegments();
      } catch (error) { message(error.message, true); } finally { saveRange.disabled = false; saveRange.textContent = 'Save range'; }
    };
    node.querySelector('[data-save-transcript]').onclick = async () => {
      try {
        await api(`/segments/${segment.id}/transcript`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({transcript:transcriptInput.value}) });
        message('Caption text saved. The tags and search data were updated too.'); state.resultMode = 'all'; await loadSegments();
      } catch (error) { message(error.message, true); }
    };
    const saveRating = async (rating) => { const review_reason = rating === 'rejected' ? reviewReason.value : ''; await api(`/segments/${segment.id}`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rating, review_reason}) }); segment.rating = rating; segment.review_reason = review_reason; const original = state.activeResults?.find((item) => item.id === segment.id); if (original) { original.rating = rating; original.review_reason = review_reason; } await reloadActiveSegments(); };
    node.querySelectorAll('[data-rating]').forEach((button) => button.onclick = () => saveRating(button.dataset.rating));
    node.querySelector('[data-save-rating]').onclick = () => saveRating(ratingSelect.value);
    node.querySelector('[data-example]').onclick = async () => { if (!state.collectionId) return message('Choose a reference collection first.', true); await api(`/collections/${state.collectionId}/examples`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({segment_id:segment.id}) }); message('Reference added.'); await refreshLibrary(); };
    const preview = node.querySelector('[data-preview]'); const player = node.querySelector('.audio-preview');
    player.onplay = () => { if (state.previewAudio && state.previewAudio !== player) state.previewAudio.pause(); state.previewAudio = player; };
    player.onpause = () => { if (state.previewAudio === player) state.previewAudio = null; };
    player.onended = () => { if (state.previewAudio === player) state.previewAudio = null; };
    player.onerror = () => message('Audio preview could not be played.', true);
    preview.onclick = async () => { player.src = `/api/segments/${segment.id}/audio-preview?audio_track=${encodeURIComponent(state.globalExport.audio_track)}`; player.hidden = false; try { await player.play(); } catch { message('Audio preview could not be started.', true); } };
    const exportButton = node.querySelector('[data-export]'); exportButton.disabled = segment.rating !== 'accepted'; exportButton.textContent = segment.rating === 'accepted' ? 'Export MP4' : 'Approve before export'; exportButton.onclick = () => { const position = state.captionPositions[segment.id] || captionPosition.value; const settings = state.globalCaption; const output = state.globalExport; const filename = state.exportNames[segment.id] || exportName.value; window.location.href = `/api/segments/${segment.id}/export?captions_preset=${encodeURIComponent(settings.captions_preset)}&caption_position=${encodeURIComponent(position)}&base_color=${encodeURIComponent(settings.base_color)}&active_color=${encodeURIComponent(settings.active_color)}&layout=${encodeURIComponent(output.layout)}&audio_track=${encodeURIComponent(output.audio_track)}&filename=${encodeURIComponent(filename)}`; }; box.append(node);
  }
}

function addRejectionReasons(select) {
  for (const reason of state.rejectionReasons) {
    if ([...select.options].some((option) => option.value === reason)) continue;
    const option = document.createElement('option'); option.value = reason; option.textContent = reason; select.append(option);
  }
}

function clearClipEditor() {
  state.editingSegment = null;
  $('#clip-editor-title').textContent = 'Select a clip to edit it.';
  $('#clip-editor-empty').hidden = false;
  $('#clip-editor-form').hidden = true;
  const player = $('#editor-audio-preview');
  player.pause(); player.removeAttribute('src'); player.hidden = true;
  if (state.previewAudio === player) state.previewAudio = null;
}

function selectClipForEditor(segment) {
  state.editingSegment = segment;
  $('#clip-editor-title').textContent = `${fmt(segment.start_seconds)} - ${fmt(segment.end_seconds)}`;
  $('#clip-editor-empty').hidden = true;
  $('#clip-editor-form').hidden = false;
  $('#editor-start').value = Number(segment.start_seconds).toFixed(1);
  $('#editor-end').value = Number(segment.end_seconds).toFixed(1);
  $('#editor-caption-position').value = state.captionPositions[segment.id] || 'bottom';
  $('#editor-rating-select').value = segment.rating || 'unrated';
  const reviewReason = $('#editor-review-reason'); addRejectionReasons(reviewReason);
  if ([...reviewReason.options].some((option) => option.value === segment.review_reason)) reviewReason.value = segment.review_reason;
  $('#editor-transcript').value = segment.transcript || '';
  $('#editor-censor-profanity').checked = Boolean(segment.censor_profanity);
  $('#editor-export-name').value = state.exportNames[segment.id] || '';
  const exportButton = $('#editor-export'); exportButton.disabled = segment.rating !== 'accepted'; exportButton.textContent = segment.rating === 'accepted' ? 'Export MP4' : 'Approve before export';
  document.querySelectorAll('.segment').forEach((article) => article.classList.toggle('editing', article.dataset.segmentId === segment.id));
}

async function saveSegmentRating(segment, rating) {
  const review_reason = rating === 'rejected' ? $('#editor-review-reason').value : '';
  await api(`/segments/${segment.id}`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rating, review_reason}) });
  segment.rating = rating; segment.review_reason = review_reason;
  const original = state.activeResults?.find((item) => item.id === segment.id);
  if (original) { original.rating = rating; original.review_reason = review_reason; }
  await reloadActiveSegments();
}

async function playClipAudio(segment) {
  selectClipForEditor(segment);
  const player = $('#editor-audio-preview');
  player.src = `/api/segments/${segment.id}/audio-preview?audio_track=${encodeURIComponent(state.globalExport.audio_track)}`;
  player.hidden = false;
  try { await player.play(); } catch { message('Audio preview could not be started.', true); }
}

async function loadSegments(custom = null) {
  if (!state.videoId) return;
  const selectedTag = $('#tag-search').value; const selectedRating = $('#rating-search').value; const hideReading = $('#hide-reading').checked && selectedTag !== 'reading';
  const values = custom
    ? custom.filter((segment) => (!selectedTag || (segment.tags || []).includes(selectedTag)) && (!selectedRating || segment.rating === selectedRating) && (!hideReading || !(segment.tags || []).includes('reading')))
    : await api(`/videos/${state.videoId}/segments?q=${encodeURIComponent($('#search').value)}&tag=${encodeURIComponent(selectedTag)}&rating=${encodeURIComponent(selectedRating)}&hide_reading=${hideReading}`);
  const box = $('#segments'); const editingId = state.editingSegment?.id; let refreshedEditingSegment = null; box.replaceChildren();
  if (!values.length) { clearClipEditor(); box.append(make('p', 'hint', 'No candidates yet. Analysis may still be running.')); return; }
  const template = $('#segment-template');
  for (const segment of values) {
    const node = template.content.cloneNode(true); const article = node.querySelector('article'); article.dataset.segmentId = segment.id; article.classList.add(segment.rating);
    const score = segment.similarity !== undefined ? ` / prompt ${Math.round(segment.similarity * 100)}%` : '';
    node.querySelector('.time').textContent = `${fmt(segment.start_seconds)} - ${fmt(segment.end_seconds)}${score}`;
    node.querySelector('.ranking').textContent = segment.ranking_score ? `Suggested score ${segment.ranking_score}/99 - ${segment.ranking_reason}` : `Clip quality ${segment.quality_score || 0}/99${(segment.quality_signals || []).length ? ` - ${(segment.quality_signals || []).join(', ')}` : ''}`;
    const tags = node.querySelector('.tags');
    for (const tag of segment.tags || []) tags.append(make('span', 'tag', tag));
    node.querySelector('.transcript').textContent = segment.transcript || 'No recognized speech';
    node.querySelector('[data-open]').onclick = () => openSegmentInRecording(segment);
    node.querySelectorAll('[data-rating]').forEach((button) => button.onclick = () => { selectClipForEditor(segment); saveSegmentRating(segment, button.dataset.rating).catch((error) => message(error.message, true)); });
    node.querySelector('[data-example]').onclick = async () => { if (!state.collectionId) return message('Choose a reference collection first.', true); await api(`/collections/${state.collectionId}/examples`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({segment_id:segment.id}) }); message('Reference added.'); await refreshLibrary(); };
    node.querySelector('[data-preview]').onclick = () => { playClipAudio(segment); };
    article.onclick = (event) => { if (!event.target.closest('button, a, audio, input, textarea, select, label')) selectClipForEditor(segment); };
    if (segment.id === editingId) refreshedEditingSegment = segment;
    box.append(node);
  }
  if (refreshedEditingSegment) selectClipForEditor(refreshedEditingSegment);
  else if (editingId) clearClipEditor();
}

async function reloadActiveSegments() {
  await loadSegments(state.resultMode === 'all' ? null : (state.activeResults || []));
}

async function loadCollections() {
  const collections = await api('/collections'); const box = $('#collections'); box.replaceChildren();
  if (state.collectionId && !collections.some((item) => item.id === state.collectionId)) { state.collectionId = null; state.collectionName = ''; }
  for (const collection of collections) {
    const button = make('button', `collection ${state.collectionId === collection.id ? 'selected' : ''}`, `${collection.name} (${collection.examples})`);
    button.onclick = async () => { state.collectionId = collection.id; state.collectionName = collection.name; updateSelectionSummary(); await loadCollections(); await loadImportStatus(); };
    box.append(button);
  }
  updateSelectionSummary();
}

async function loadPrompts() {
  const prompts = await api('/prompts'); const box = $('#prompts'); box.replaceChildren();
  for (const prompt of prompts) {
    const row = make('div', 'prompt-row'); const use = make('button', 'quiet', prompt.name); use.onclick = () => { $('#active-prompt').value = prompt.prompt; updateSelectionSummary(); };
    const remove = make('button', 'quiet', 'Delete'); remove.onclick = async () => { await api(`/prompts/${prompt.id}`, { method:'DELETE' }); await loadPrompts(); };
    row.append(use, remove); box.append(row);
  }
}

async function loadCaptionSettings() {
  const [defaults, favorites] = await Promise.all([api('/caption-defaults'), api('/caption-favorites')]);
  if (!state.captionDirty) {
    state.globalCaption = defaults;
    $('#global-caption-preset').value = defaults.captions_preset;
    $('#global-caption-base-color').value = defaults.base_color.toLowerCase();
    $('#global-caption-active-color').value = defaults.active_color.toLowerCase();
  }
  const box = $('#caption-favorites'); box.replaceChildren();
  for (const favorite of favorites) {
    const row = make('div', 'prompt-row');
    const use = make('button', 'quiet', `${favorite.name} (${favorite.captions_preset})`);
    use.onclick = async () => {
      try {
        state.globalCaption = await api('/caption-defaults', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({captions_preset:favorite.captions_preset, base_color:favorite.base_color, active_color:favorite.active_color}) }); state.captionDirty = false;
        await loadCaptionSettings(); message(`Caption favorite “${favorite.name}” applied.`);
      } catch (error) { message(error.message, true); }
    };
    const remove = make('button', 'quiet', 'Delete');
    remove.onclick = async () => { try { await api(`/caption-favorites/${favorite.id}`, { method:'DELETE' }); await loadCaptionSettings(); } catch (error) { message(error.message, true); } };
    row.append(use, remove); box.append(row);
  }
}

async function loadExportSettings() {
  const defaults = await api('/export-defaults');
  if (!state.exportDirty) {
    state.globalExport = { layout: defaults.layout, audio_track: Number(defaults.audio_track) };
    $('#global-layout').value = state.globalExport.layout;
    $('#global-audio-track').value = String(state.globalExport.audio_track);
  }
}

function updateAnalysisAudioModeUi() {
  const split = $('#analysis-audio-mode').value === 'split';
  $('#analysis-single-track-wrap').hidden = split;
  $('#analysis-split-options').hidden = !split;
  $('#analysis-all-sounds-track').disabled = !split || !$('#analysis-use-all-sounds').checked;
  $('#analysis-game-track').disabled = !split || !$('#analysis-use-game').checked;
}

async function loadAnalysisAudioSettings() {
  const defaults = await api('/analysis-audio-defaults');
  // The dashboard refreshes in the background. Never let it overwrite a
  // track/switch the user has changed but not saved yet.
  if (state.analysisAudioDirty) return;
  state.analysisAudio = { ...defaults, use_all_sounds:Boolean(defaults.use_all_sounds), use_game:Boolean(defaults.use_game) };
  $('#analysis-audio-mode').value = defaults.mode;
  $('#analysis-single-track').value = String(defaults.single_track);
  $('#analysis-microphone-track').value = String(defaults.microphone_track);
  $('#analysis-all-sounds-track').value = String(defaults.all_sounds_track);
  $('#analysis-game-track').value = String(defaults.game_track);
  $('#analysis-use-all-sounds').checked = Boolean(defaults.use_all_sounds);
  $('#analysis-use-game').checked = Boolean(defaults.use_game);
  updateAnalysisAudioModeUi();
}

async function loadDiscoverySettings() {
  const defaults = await api('/discovery-defaults');
  state.discovery = defaults;
  const select = $('#discovery-profile'); const previous = select.value; select.replaceChildren();
  for (const profile of defaults.profiles) { const option = document.createElement('option'); option.value = profile.id; option.textContent = profile.name; select.append(option); }
  select.value = defaults.active_profile || previous || 'general';
}

function rememberAnalysisAudio() {
  state.analysisAudio = {
    mode: $('#analysis-audio-mode').value,
    single_track: Number($('#analysis-single-track').value),
    microphone_track: Number($('#analysis-microphone-track').value),
    all_sounds_track: Number($('#analysis-all-sounds-track').value),
    game_track: Number($('#analysis-game-track').value),
    use_all_sounds: $('#analysis-use-all-sounds').checked,
    use_game: $('#analysis-use-game').checked,
  };
  state.analysisAudioDirty = true;
  updateAnalysisAudioModeUi();
}

async function loadReferenceSources() {
  const sources = await api('/reference-sources'); const box = $('#reference-sources'); box.replaceChildren();
  for (const source of sources) {
    const row = make('div', 'source-row'); row.append(make('strong', '', `${source.collection_name} (${source.imported_examples} clips)`), make('p', '', source.folder_path));
    const use = make('button', 'quiet', 'Use collection'); use.onclick = async () => { state.collectionId = source.collection_id; state.collectionName = source.collection_name; updateSelectionSummary(); await loadCollections(); await loadImportStatus(); };
    const reimport = make('button', 'quiet', 'Reimport folder'); reimport.onclick = async () => { await api(`/reference-sources/${source.id}/imports`, { method:'POST' }); message('Saved folder queued for import.'); state.collectionId = source.collection_id; state.collectionName = source.collection_name; await loadImportStatus(); };
    row.append(use, reimport); box.append(row);
  }
}

async function loadImportStatus() {
  const box = $('#import-status'); box.replaceChildren(); if (!state.collectionId) return;
  const imports = await api(`/collections/${state.collectionId}/imports`);
  for (const item of imports) { const row = make('div', 'import-row', `${item.state}: ${item.message}`); const track = make('div', 'progress-track'); const fill = make('div', 'progress-fill'); fill.style.width = `${clamp(item.progress)}%`; track.append(fill); row.append(track); box.append(row); }
}

async function loadRejectionReasons() { state.rejectionReasons = (await api('/rejection-reasons')).map((item) => item.reason); const box = $('#saved-rejection-reasons'); box.replaceChildren(); for (const reason of state.rejectionReasons) box.append(make('div', 'hint', reason)); if (state.editingSegment) addRejectionReasons($('#editor-review-reason')); }
async function refreshLibrary() { await Promise.all([loadCollections(), loadPrompts(), loadReferenceSources(), loadCaptionSettings(), loadExportSettings(), loadAnalysisAudioSettings(), loadDiscoverySettings(), loadRejectionReasons()]); if (state.collectionId) await loadImportStatus(); }
async function refreshDashboard() {
  try { await Promise.all([loadVideos(), refreshLibrary()]); const current = state.videos.find((video) => video.id === state.videoId); const editing = document.activeElement?.matches('input, textarea, select'); if (current?.status === 'ready' && state.resultMode === 'all' && !state.previewAudio && !editing) await loadSegments(); if (Date.now() >= state.statusErrorUntil) message('Local API online'); } catch (error) { message(`Reconnecting to local API: ${error.message}`, true); }
}

function selectedEditorSegment() {
  if (state.editingSegment) return state.editingSegment;
  message('Select a clip first.', true); return null;
}

$('#editor-caption-position').onchange = () => { const segment = selectedEditorSegment(); if (segment) state.captionPositions[segment.id] = $('#editor-caption-position').value; };
$('#editor-export-name').oninput = () => { const segment = selectedEditorSegment(); if (segment) state.exportNames[segment.id] = $('#editor-export-name').value; };
$('#editor-save-range').onclick = async () => {
  const segment = selectedEditorSegment(); if (!segment) return;
  const start_seconds = Number($('#editor-start').value); const end_seconds = Number($('#editor-end').value);
  if (!Number.isFinite(start_seconds) || !Number.isFinite(end_seconds) || end_seconds <= start_seconds) return message('Enter a valid clip range.', true);
  const button = $('#editor-save-range'); button.disabled = true; button.textContent = 'Updating captions...';
  try { const updated = await api(`/segments/${segment.id}/timing`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({start_seconds, end_seconds}) }); Object.assign(segment, updated); const original = state.activeResults?.find((item) => item.id === segment.id); if (original) Object.assign(original, updated); message('Clip range and captions updated.'); await reloadActiveSegments(); }
  catch (error) { message(error.message, true); } finally { button.disabled = false; button.textContent = 'Save range'; }
};
$('#editor-save-transcript').onclick = async () => {
  const segment = selectedEditorSegment(); if (!segment) return;
  const button = $('#editor-save-transcript'); button.disabled = true;
  try { const updated = await api(`/segments/${segment.id}/transcript`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({transcript:$('#editor-transcript').value}) }); Object.assign(segment, updated); const original = state.activeResults?.find((item) => item.id === segment.id); if (original) Object.assign(original, updated); message('Caption text saved. The tags and search data were updated too.'); await reloadActiveSegments(); }
  catch (error) { message(error.message, true); } finally { button.disabled = false; }
};
$('#editor-save-rating').onclick = async () => { const segment = selectedEditorSegment(); if (!segment) return; try { await saveSegmentRating(segment, $('#editor-rating-select').value); } catch (error) { message(error.message, true); } };
$('#editor-censor-profanity').onchange = async () => {
  const segment = selectedEditorSegment(); if (!segment) return;
  const toggle = $('#editor-censor-profanity'); toggle.disabled = true;
  try { const updated = await api(`/segments/${segment.id}/censor`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({censor_profanity:toggle.checked}) }); Object.assign(segment, updated); const original = state.activeResults?.find((item) => item.id === segment.id); if (original) Object.assign(original, updated); message(updated.censor_profanity ? 'Profanity censoring enabled for this clip export.' : 'Profanity censoring disabled for this clip export.'); }
  catch (error) { toggle.checked = !toggle.checked; message(error.message, true); } finally { toggle.disabled = false; }
};
$('#editor-export').onclick = () => {
  const segment = selectedEditorSegment(); if (!segment || segment.rating !== 'accepted') return;
  const position = state.captionPositions[segment.id] || $('#editor-caption-position').value; const settings = state.globalCaption; const output = state.globalExport; const filename = state.exportNames[segment.id] || $('#editor-export-name').value;
  window.location.href = `/api/segments/${segment.id}/export?captions_preset=${encodeURIComponent(settings.captions_preset)}&caption_position=${encodeURIComponent(position)}&base_color=${encodeURIComponent(settings.base_color)}&active_color=${encodeURIComponent(settings.active_color)}&layout=${encodeURIComponent(output.layout)}&audio_track=${encodeURIComponent(output.audio_track)}&filename=${encodeURIComponent(filename)}`;
};
const editorPlayer = $('#editor-audio-preview');
editorPlayer.onplay = () => { if (state.previewAudio && state.previewAudio !== editorPlayer) state.previewAudio.pause(); state.previewAudio = editorPlayer; };
editorPlayer.onpause = () => { if (state.previewAudio === editorPlayer) state.previewAudio = null; };
editorPlayer.onended = () => { if (state.previewAudio === editorPlayer) state.previewAudio = null; };
editorPlayer.onerror = () => message('Audio preview could not be played.', true);

$('#upload-form').onsubmit = async (event) => { event.preventDefault(); const file = $('#video-file').files[0]; if (!file) return message('Choose a video file first.', true); const submit = event.target.querySelector('button'); submit.disabled = true; setUploadProgress(0, `Preparing ${file.name} for upload...`); try { await uploadVideo(file); setUploadProgress(100, 'Upload complete. Analysis runs in the background.'); message('Upload complete. Analysis runs in the background.'); event.target.reset(); await refreshDashboard(); } catch (error) { setUploadProgress(0, error.message, true); message(error.message, true); } finally { submit.disabled = false; } };
$('#remote-video-form').onsubmit = async (event) => { event.preventDefault(); const url = $('#remote-video-url').value.trim(); const submit = event.target.querySelector('button'); if (!url) return message('Paste a YouTube or Twitch VOD link first.', true); submit.disabled = true; try { await api('/videos/from-url', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({source_url:url}) }); event.target.reset(); message('Download queued. Its progress appears in Recordings.'); await refreshDashboard(); } catch (error) { message(error.message, true); } finally { submit.disabled = false; } };
$('#rejection-reason-form').onsubmit = async (event) => { event.preventDefault(); const input = $('#new-rejection-reason'); const reason = input.value.trim(); if (!reason) return message('Enter a custom rejection reason first.', true); const button = $('#save-rejection-reason'); button.disabled = true; try { const saved = await api('/rejection-reasons', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({reason}) }); input.value = ''; await loadRejectionReasons(); document.querySelectorAll('[data-review-reason]').forEach((select) => { if (![...select.options].some((option) => option.value === saved.reason)) { const option = document.createElement('option'); option.value = saved.reason; option.textContent = saved.reason; select.append(option); } }); message(`Custom rejection reason saved: ${saved.reason}`); } catch (error) { message(error.message, true); } finally { button.disabled = false; } };
$('#collection-form').onsubmit = async (event) => { event.preventDefault(); try { const collection = await api('/collections', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:$('#collection-name').value})}); state.collectionId = collection.id; state.collectionName = collection.name; event.target.reset(); await refreshLibrary(); } catch (error) { message(error.message, true); } };
$('#prompt-form').onsubmit = async (event) => { event.preventDefault(); try { const prompt = await api('/prompts', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:$('#prompt-name').value, prompt:$('#prompt-text').value}) }); $('#active-prompt').value = prompt.prompt; event.target.reset(); updateSelectionSummary(); await loadPrompts(); } catch (error) { message(error.message, true); } };
function rememberGlobalCaption() { state.globalCaption = { captions_preset:$('#global-caption-preset').value, base_color:$('#global-caption-base-color').value, active_color:$('#global-caption-active-color').value }; state.captionDirty = true; }
$('#global-caption-preset').onchange = rememberGlobalCaption;
$('#global-caption-base-color').oninput = rememberGlobalCaption;
$('#global-caption-active-color').oninput = rememberGlobalCaption;
$('#caption-defaults-form').onsubmit = async (event) => { event.preventDefault(); try { state.globalCaption = await api('/caption-defaults', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({captions_preset:$('#global-caption-preset').value, base_color:$('#global-caption-base-color').value, active_color:$('#global-caption-active-color').value}) }); state.captionDirty = false; await loadCaptionSettings(); message('Global caption settings saved.'); } catch (error) { message(error.message, true); } };
$('#caption-favorite-form').onsubmit = async (event) => { event.preventDefault(); try { await api('/caption-favorites', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:$('#caption-favorite-name').value, captions_preset:$('#global-caption-preset').value, base_color:$('#global-caption-base-color').value, active_color:$('#global-caption-active-color').value}) }); event.target.reset(); await loadCaptionSettings(); message('Caption favorite saved.'); } catch (error) { message(error.message, true); } };
function rememberGlobalExport() { state.globalExport = { layout: $('#global-layout').value, audio_track: Number($('#global-audio-track').value) }; state.exportDirty = true; }
$('#global-layout').onchange = rememberGlobalExport;
$('#global-audio-track').onchange = rememberGlobalExport;
$('#export-defaults-form').onsubmit = async (event) => { event.preventDefault(); try { state.globalExport = await api('/export-defaults', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({layout:$('#global-layout').value, audio_track:Number($('#global-audio-track').value)}) }); state.exportDirty = false; await loadExportSettings(); message('Global export settings saved.'); } catch (error) { message(error.message, true); } };
['#analysis-audio-mode', '#analysis-single-track', '#analysis-microphone-track', '#analysis-all-sounds-track', '#analysis-game-track', '#analysis-use-all-sounds', '#analysis-use-game'].forEach((selector) => { $(selector).onchange = rememberAnalysisAudio; });
$('#analysis-audio-form').onsubmit = async (event) => { event.preventDefault(); const body = { mode:$('#analysis-audio-mode').value, single_track:Number($('#analysis-single-track').value), microphone_track:Number($('#analysis-microphone-track').value), all_sounds_track:Number($('#analysis-all-sounds-track').value), game_track:Number($('#analysis-game-track').value), use_all_sounds:$('#analysis-use-all-sounds').checked, use_game:$('#analysis-use-game').checked }; try { state.analysisAudio = await api('/analysis-audio-defaults', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) }); state.analysisAudioDirty = false; await loadAnalysisAudioSettings(); message('Analysis audio settings saved. They apply to the next analysis or reanalysis.'); } catch (error) { message(error.message, true); } };
$('#discovery-defaults-form').onsubmit = async (event) => { event.preventDefault(); try { state.discovery = await api('/discovery-defaults', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({active_profile:$('#discovery-profile').value}) }); await loadDiscoverySettings(); if (state.videoId) await showAllSegments(); message('Discovery profile saved. Candidate ranking was refreshed.'); } catch (error) { message(error.message, true); } };
$('#generate-prompt-button').onclick = async () => { if (!state.collectionId) return; try { const suggested = await api(`/collections/${state.collectionId}/prompt-suggestion`, { method:'POST' }); $('#prompt-name').value = suggested.name; $('#prompt-text').value = suggested.prompt; $('#active-prompt').value = suggested.prompt; updateSelectionSummary(); message('Prompt generated from reference clips. Review it and click Save prompt.'); } catch (error) { message(error.message, true); } };
$('#import-folder-button').onclick = async () => { const folder_path = $('#reference-folder').value.trim(); if (!state.collectionId || !folder_path) return message('Choose a collection and enter the folder path.', true); try { await api(`/collections/${state.collectionId}/imports`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({folder_path, include_subfolders:$('#subfolders').checked}) }); message('Folder saved and queued for import.'); await loadReferenceSources(); await loadImportStatus(); } catch (error) { message(error.message, true); } };
function showAllSegments() { state.resultMode = 'all'; state.activeResults = null; loadSegments(); }
$('#search-button').onclick = showAllSegments;
$('#tag-search-button').onclick = showAllSegments;
$('#rating-search-button').onclick = showAllSegments;
$('#tag-search').onchange = showAllSegments;
$('#rating-search').onchange = showAllSegments;
$('#hide-reading').onchange = showAllSegments;
$('#show-duplicates').onchange = showAllSegments;
$('#top-clips-button').onclick = async () => { if (!state.videoId) return message('Choose a recording first.', true); try { const results = await api(`/videos/${state.videoId}/top-clips?limit=10`); state.resultMode = 'top'; state.activeResults = results; await loadSegments(results); message('Showing the 10 strongest different candidates.'); } catch (error) { message(error.message, true); } };
$('#description-button').onclick = async () => { const description = $('#active-prompt').value.trim(); if (!state.videoId || description.length < 3) return message('Choose a recording and enter a prompt first.', true); try { const results = await api('/search/description', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({video_id:state.videoId, description}) }); state.resultMode = 'description'; state.activeResults = results; await loadSegments(results); } catch (error) { message(error.message, true); } };
$('#similar-button').onclick = async () => { if (!state.videoId || !state.collectionId) return message('Choose a recording and a collection first.', true); try { const results = await api(`/collections/${state.collectionId}/search`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({video_id:state.videoId})}); state.resultMode = 'similar'; state.activeResults = results; await loadSegments(results); } catch (error) { message(error.message, true); } };
$('#active-prompt').oninput = updateSelectionSummary; $('#refresh').onclick = refreshDashboard;
function setSetupSidebar(open) {
  const sidebar = $('#setup-sidebar'); const trigger = $('#setup-toggle');
  sidebar.classList.toggle('open', open);
  document.querySelector('main').classList.toggle('setup-sidebar-open', open);
  sidebar.setAttribute('aria-hidden', String(!open));
  trigger.setAttribute('aria-expanded', String(open));
}
$('#setup-toggle').onclick = () => setSetupSidebar(!$('#setup-sidebar').classList.contains('open'));
$('#setup-close').onclick = () => setSetupSidebar(false);
$('#clip-editor-close').onclick = () => setClipEditorOpen(false);
$('#clip-editor-toggle').onclick = () => setClipEditorOpen(true);
try { setClipEditorOpen(localStorage.getItem('clipfinder-clip-editor-open') !== 'false'); } catch { setClipEditorOpen(true); }
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') setSetupSidebar(false); });
$('#close-dialog').onclick = () => { $('#full-video').pause(); $('#video-dialog').close(); };
api('/health').then(refreshDashboard).catch(() => message('Local API unavailable', true)); setInterval(refreshDashboard, 4000);
