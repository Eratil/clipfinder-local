const state = { videoId: null, collectionId: null, collectionName: '', videos: [], rejectionReasons: [], analysisAudio: { mode:'split', single_track:1, microphone_track:2, all_sounds_track:1, game_track:3, use_all_sounds:true, use_game:true }, discovery: { active_profile:'general', profiles:[] }, chat: null, resultMode: 'all', activeResults: null, previewAudio: null, listeningSegment: null, listenAudioTrack: 1, quickReview: { clips: [], index: 0, saving: false }, editingSegment: null, clipEditorOpen: false, captionPositions: {}, exportNames: {}, globalCaption: { captions_preset: 'highlight', base_color: '#FFFFFF', active_color: '#FFFF00' }, globalExport: { layout: 'original', audio_track: 1, camera_x:.78, camera_y:.03, camera_width:.11, camera_height:.11, game_x:.22, game_y:0, game_width:.56, game_height:1 }, layoutCalibration: { mode:'camera', drawing:null }, captionDirty: false, exportDirty: false, analysisAudioDirty: false, statusErrorUntil: 0, updateDownloadId: null };
const $ = (selector) => document.querySelector(selector);
const fmt = (seconds) => new Date(seconds * 1000).toISOString().slice(11, 19);
const elapsed = (seconds) => {
  const total = Math.max(0, Math.round(Number(seconds || 0)));
  const hours = Math.floor(total / 3600); const minutes = Math.floor((total % 3600) / 60); const rest = total % 60;
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
};
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
async function loadRuntimeStatus() {
  const runtime = await api('/runtime-status');
  $('#runtime-headline').textContent = runtime.headline;
  $('#app-version').textContent = `Version ${runtime.version || '--'}`;
  const gpu = runtime.gpu ? `${runtime.gpu.name} / ${runtime.gpu.memory_mb} MB VRAM` : 'No NVIDIA GPU detected';
  $('#runtime-detail').textContent = `Transcription: ${runtime.transcription.label}. Similarity search: ${runtime.embeddings.label}. ${gpu}.`;
  $('#runtime-headline').classList.toggle('runtime-warning', runtime.transcription.mode === 'unavailable');
}
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
    const analysisTime = Number(video.analysis_seconds || 0) > 0 ? ` / analysis: ${elapsed(video.analysis_seconds)}` : '';
    const estimate = Number(video.estimated_analysis_seconds || 0) > 0 ? ` / estimated analysis: ~${elapsed(video.estimated_analysis_seconds)} (based on ${video.estimate_sample_count} previous)` : '';
    info.append(make('p', 'video-meta', `${video.duration_seconds ? fmt(video.duration_seconds) : '--:--:--'} / ${bytes(video.size_bytes)}${analysisTime}${estimate} / ${video.message || video.status}`));
    const progress = make('div', 'video-progress'); const track = make('div', 'progress-track'); const fill = make('div', 'progress-fill'); fill.style.width = `${clamp(video.progress)}%`; track.append(fill); progress.append(track, make('strong', '', `${clamp(video.progress)}%`));
    card.append(info, make('span', 'pill', video.status), progress);
    if (['failed', 'interrupted', 'ready'].includes(video.status)) { const label = video.status === 'ready' ? 'Reanalyze recording' : 'Run analysis again'; const retry = make('button', 'quiet', label); retry.onclick = async (event) => { event.stopPropagation(); await api(`/videos/${video.id}/analyse`, { method:'POST' }); message('Analysis queued again.'); await refreshDashboard(); }; const remove = make('button', 'quiet danger-button', 'Delete recording'); remove.onclick = async (event) => { event.stopPropagation(); if (!window.confirm(`Delete ${video.original_name}, its analysis data and ${bytes(video.size_bytes)} of source video? Exported clips will be kept.`)) return; remove.disabled = true; try { await api(`/videos/${video.id}`, { method:'DELETE' }); if (state.videoId === video.id) { state.videoId = null; state.activeResults = null; $('#workspace').hidden = true; } message('Source recording and its analysis data deleted. Exported clips were kept.'); await refreshDashboard(); } catch (error) { message(error.message, true); remove.disabled = false; } }; card.append(retry, remove); }
    card.onclick = () => selectVideo(video); box.append(card);
  }
}

async function selectVideo(video) {
  state.videoId = video.id; state.resultMode = 'all'; state.activeResults = null; state.captionPositions = {}; state.exportNames = {}; clearClipEditor(); $('#workspace').hidden = false; $('#selected-title').textContent = `Candidates: ${video.original_name}`;
  updateSelectionSummary(); await Promise.all([loadVideos(), loadSegments(), loadChatSummary()]);
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
    renderClipContext(node, segment);
    renderChatReaction(node, segment);
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
    preview.onclick = async () => { player.src = `/api/segments/${segment.id}/audio-preview?audio_track=${encodeURIComponent(state.listenAudioTrack)}`; player.hidden = false; try { await player.play(); } catch { message('Audio preview could not be started.', true); } };
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
}

function selectClipForEditor(segment, openPanel = true) {
  if (openPanel) setClipEditorOpen(true);
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
  const player = $('#global-audio-player');
  state.listeningSegment = segment;
  $('#audio-now-playing-title').textContent = 'Listening to clip';
  $('#audio-now-playing-detail').textContent = `${fmt(segment.start_seconds)} - ${fmt(segment.end_seconds)} / ${segment.transcript || 'No recognized speech'}`;
  $('#audio-now-playing').hidden = false;
  $('#listen-audio-track').value = String(state.listenAudioTrack || 1);
  await loadListeningAudio();
}

async function loadListeningAudio() {
  const segment = state.listeningSegment; if (!segment) return;
  const player = $('#global-audio-player'); const track = Number($('#listen-audio-track').value || 1);
  state.listenAudioTrack = track;
  const source = `/api/segments/${segment.id}/audio-preview?audio_track=${encodeURIComponent(track)}`;
  try {
    await api(`/segments/${segment.id}/audio-preview?audio_track=${encodeURIComponent(track)}`);
    player.src = source; player.load(); await player.play();
  } catch (error) {
    player.pause(); player.removeAttribute('src'); player.load(); message(error.message, true);
  }
}

function currentQuickClip() {
  return state.quickReview.clips[state.quickReview.index] || null;
}

function loadQuickReviewPreview(clip) {
  const player = $('#quick-review-video');
  player.pause();
  player.removeAttribute('src');
  player.load();
  const start = Number(clip.start_seconds || 0);
  const end = Number(clip.end_seconds || start + 1);
  player.src = `/api/videos/${clip.video_id}/stream#t=${start.toFixed(2)},${end.toFixed(2)}`;
  player.onloadedmetadata = () => { player.currentTime = start; };
  player.load();
}

function clearQuickReviewPreview() {
  const player = $('#quick-review-video');
  player.pause();
  player.removeAttribute('src');
  player.load();
}

function renderQuickReview(autoListen = true) {
  const clip = currentQuickClip();
  const total = state.quickReview.clips.length;
  if (!clip || !total) return;
  const reviewed = state.quickReview.clips.filter((item) => item.rating !== 'unrated').length;
  $('#quick-review-progress').textContent = `${state.quickReview.index + 1} / ${total}  |  reviewed: ${reviewed}`;
  $('#quick-review-time').textContent = `${fmt(clip.start_seconds)} - ${fmt(clip.end_seconds)}`;
  $('#quick-review-ranking').textContent = clip.ranking_score ? `Suggested score ${clip.ranking_score}/99 - ${clip.ranking_reason}` : `Clip quality ${clip.quality_score || 0}/99`;
  const tags = $('#quick-review-tags'); tags.replaceChildren(); for (const tag of clip.tags || []) tags.append(make('span', 'tag', tag));
  $('#quick-review-transcript').textContent = clip.transcript || 'No recognized speech';
  const context = Number(clip.context_score || 0); const selfContained = Number(clip.self_contained_score || 0); const moment = Number(clip.moment_reaction_score || 0);
  $('#quick-review-context').textContent = `${context ? `Context ${context}/99` : ''}${context && selfContained ? ' / ' : ''}${selfContained ? `Self-contained ${selfContained}/99` : ''}${moment ? ` / Moment -> reaction ${moment}/30` : ''}${selfContained && selfContained <= 35 ? ' - may need surrounding speech.' : selfContained >= 75 ? ' - complete thought.' : ''}`;
  $('#quick-review-approve').disabled = state.quickReview.saving;
  $('#quick-review-reject').disabled = state.quickReview.saving;
  $('#quick-review-previous').disabled = state.quickReview.saving || state.quickReview.index === 0;
  $('#quick-review-next').disabled = state.quickReview.saving || state.quickReview.index === total - 1;
  $('#quick-review-listen').disabled = state.quickReview.saving;
  loadQuickReviewPreview(clip);
  if (autoListen) playClipAudio(clip).catch((error) => message(error.message, true));
}

async function openQuickReview() {
  if (!state.videoId) return message('Choose a recording first.', true);
  try {
    const clips = await api(`/videos/${state.videoId}/top-clips?limit=20&unrated_only=true`);
    if (!clips.length) return message('There are no unreviewed candidates left for quick selection.');
    state.quickReview = { clips, index: 0, saving: false };
    const dialog = $('#quick-review-dialog');
    if (!dialog.open) dialog.showModal();
    renderQuickReview(true);
  } catch (error) { message(error.message, true); }
}

async function rateQuickClip(rating) {
  const clip = currentQuickClip();
  if (!clip || state.quickReview.saving) return;
  state.quickReview.saving = true;
  renderQuickReview(false);
  try {
    await api(`/segments/${clip.id}`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rating, review_reason:''}) });
    clip.rating = rating; clip.review_reason = '';
    const original = state.activeResults?.find((item) => item.id === clip.id);
    if (original) { original.rating = rating; original.review_reason = ''; }
    if (state.quickReview.index < state.quickReview.clips.length - 1) state.quickReview.index += 1;
    await reloadActiveSegments();
    renderQuickReview(true);
  } catch (error) { message(error.message, true); renderQuickReview(false); }
  finally { state.quickReview.saving = false; renderQuickReview(false); }
}

function moveQuickReview(offset) {
  const next = Math.max(0, Math.min(state.quickReview.clips.length - 1, state.quickReview.index + offset));
  if (next === state.quickReview.index || state.quickReview.saving) return;
  state.quickReview.index = next;
  renderQuickReview(true);
}

function toggleQuickListening() {
  const clip = currentQuickClip();
  if (!clip) return;
  if (state.listeningSegment?.id === clip.id && !globalAudioPlayer.paused) { globalAudioPlayer.pause(); return; }
  playClipAudio(clip).catch((error) => message(error.message, true));
}

function closeQuickReview() {
  const dialog = $('#quick-review-dialog');
  if (dialog.open) dialog.close();
  clearQuickReviewPreview();
  globalAudioPlayer.pause(); globalAudioPlayer.removeAttribute('src'); globalAudioPlayer.load();
  $('#audio-now-playing').hidden = true;
}

async function loadSegments(custom = null) {
  if (!state.videoId) return;
  const selectedTag = $('#tag-search').value; const selectedRating = $('#rating-search').value; const hideReading = $('#hide-reading').checked && selectedTag !== 'reading';
  const source = custom
    ? custom.filter((segment) => (!selectedTag || (segment.tags || []).includes(selectedTag)) && (!selectedRating || segment.rating === selectedRating) && (!hideReading || !(segment.tags || []).includes('reading')))
    : await api(`/videos/${state.videoId}/segments?q=${encodeURIComponent($('#search').value)}&tag=${encodeURIComponent(selectedTag)}&rating=${encodeURIComponent(selectedRating)}&hide_reading=${hideReading}&sort=${encodeURIComponent($('#score-sort').value)}`);
  const values = sortSegments(source);
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
    renderClipContext(node, segment);
    renderChatReaction(node, segment);
    node.querySelector('[data-open]').onclick = () => openSegmentInRecording(segment);
    node.querySelectorAll('[data-rating]').forEach((button) => button.onclick = () => { selectClipForEditor(segment); saveSegmentRating(segment, button.dataset.rating).catch((error) => message(error.message, true)); });
    node.querySelector('[data-example]').onclick = async () => { if (!state.collectionId) return message('Choose a reference collection first.', true); await api(`/collections/${state.collectionId}/examples`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({segment_id:segment.id}) }); message('Reference added.'); await refreshLibrary(); };
    node.querySelector('[data-preview]').onclick = () => { playClipAudio(segment); };
    article.onclick = (event) => { if (!event.target.closest('button, a, audio, input, textarea, select, label, details')) selectClipForEditor(segment); };
    if (segment.id === editingId) refreshedEditingSegment = segment;
    box.append(node);
  }
  if (refreshedEditingSegment) selectClipForEditor(refreshedEditingSegment, state.clipEditorOpen);
  else if (editingId) clearClipEditor();
}

function sortSegments(segments) {
  const mode = $('#score-sort').value;
  const fields = {
    suggested_desc: ['ranking_score', -1], suggested_asc: ['ranking_score', 1],
    quality_desc: ['quality_score', -1], quality_asc: ['quality_score', 1],
    self_contained_desc: ['self_contained_score', -1], self_contained_asc: ['self_contained_score', 1],
  };
  const [field, direction] = fields[mode] || fields.suggested_desc;
  return [...segments].sort((left, right) => (Number(left[field] || 0) - Number(right[field] || 0)) * direction);
}

async function reloadActiveSegments() {
  await loadSegments(state.resultMode === 'all' ? null : (state.activeResults || []));
}

function renderClipContext(node, segment) {
  const summary = node.querySelector('.context-reaction'); const details = node.querySelector('.clip-context');
  const score = Number(segment.context_score || 0); const selfContained = Number(segment.self_contained_score || 0); const moment = Number(segment.moment_reaction_score || 0);
  if (!score && !selfContained && !moment) { summary.hidden = true; details.hidden = true; return; }
  const description = selfContained >= 75 ? 'works without surrounding conversation' : selfContained && selfContained <= 35 ? 'may need the surrounding conversation to make sense' : 'nearby speech was checked';
  summary.textContent = `${score ? `Context ${score}/99` : ''}${score && selfContained ? ' / ' : ''}${selfContained ? `Self-contained ${selfContained}/99` : ''}${moment ? ` / Moment -> reaction ${moment}/30` : ''} - ${description}`;
  summary.hidden = false;
  const before = String(segment.context_before || '').trim(); const after = String(segment.context_after || '').trim();
  if (!before && !after) { details.hidden = true; return; }
  node.querySelector('.context-before').textContent = before ? `Before: ${before}` : 'Before: no recognised speech in the preceding 12 seconds.';
  node.querySelector('.context-after').textContent = after ? `After: ${after}` : 'After: no recognised speech in the following 12 seconds.';
  details.hidden = false;
}

function renderChatReaction(node, segment) {
  const reaction = node.querySelector('.chat-reaction'); const messages = node.querySelector('.chat-messages');
  const score = Number(segment.chat_reaction_score || 0); const joy = Number(segment.chat_joy_score || 0); const count = Number(segment.chat_message_count || 0); const people = Number(segment.chat_unique_authors || 0); const surge = Number(segment.chat_surge || 0);
  if (!score || !count) { reaction.hidden = true; messages.hidden = true; return; }
  const multiplier = surge >= 1.2 ? ` / ${surge.toFixed(1)}x normal activity` : '';
  const positive = joy >= 4 ? ` / positive-funny reaction ${joy}/14` : '';
  reaction.textContent = `Chat reaction ${score}/20 - ${count} messages${people ? ` from ${people} people` : ''}${multiplier}${positive}`;
  reaction.hidden = false;
  const previews = (segment.chat_messages || []).slice(0, 3).map((item) => `${item.author ? `${item.author}: ` : ''}${item.message}`).filter(Boolean);
  messages.textContent = previews.join('  |  '); messages.hidden = !previews.length;
}

function renderChatSummary(summary) {
  state.chat = summary;
  const box = $('#chat-summary'); const delay = $('#chat-delay');
  if (!summary?.available) { box.textContent = 'No chat transcript imported for this recording yet.'; return; }
  if (document.activeElement !== delay) delay.value = Number(summary.delay_seconds || 0);
  const authors = summary.unique_authors ? ` / ${summary.unique_authors} named viewers` : '';
  box.textContent = `${summary.source_name}: ${summary.message_count} messages${authors}. Delay: ${Number(summary.delay_seconds || 0).toFixed(1)} s.`;
}

async function loadChatSummary() {
  if (!state.videoId) return;
  try { renderChatSummary(await api(`/videos/${state.videoId}/chat`)); }
  catch (error) { $('#chat-summary').textContent = `Chat data unavailable: ${error.message}`; }
}

async function checkForUpdates() {
  const status = $('#update-status'); const button = $('#check-updates'); const download = $('#download-update'); const install = $('#install-update');
  button.disabled = true; download.hidden = true; install.hidden = true; $('#update-download-progress').hidden = true; status.textContent = 'Checking GitHub releases...';
  try {
    const update = await api('/update-status');
    if (update.error) { status.textContent = `Version ${update.current_version}: ${update.error}`; return; }
    if (!update.update_available) { status.textContent = `ClipFinder ${update.current_version} is up to date (latest: ${update.latest_version}).`; return; }
    const size = update.asset_size ? ` (${bytes(update.asset_size)})` : '';
    download.href = update.download_url; download.hidden = !update.download_url;
    if (update.automatic_install_available) {
      status.textContent = `Update available: ${update.current_version} -> ${update.latest_version}${size}.`;
      install.textContent = 'Download update'; install.onclick = startAutomaticUpdate; install.hidden = false;
    } else {
      status.textContent = `Update available: ${update.current_version} -> ${update.latest_version}${size}. Download the installer, close ClipFinder, then run it to update in place.`;
    }
  } catch (error) { status.textContent = `Could not check for updates: ${error.message}`; }
  finally { button.disabled = false; }
}

async function startAutomaticUpdate() {
  const status = $('#update-status'); const install = $('#install-update'); const progress = $('#update-download-progress'); const fill = $('#update-download-fill');
  try {
    install.disabled = true; status.textContent = 'Preparing update download...'; progress.hidden = false; fill.style.width = '0%';
    const job = await api('/updates/download', { method:'POST' }); state.updateDownloadId = job.id;
    const poll = async () => {
      try {
        const current = await api(`/updates/downloads/${state.updateDownloadId}`);
        const percent = Number(current.progress || 0); fill.style.width = `${percent}%`;
        const amount = current.total_bytes ? ` ${bytes(current.downloaded_bytes)} / ${bytes(current.total_bytes)}` : '';
        status.textContent = `${current.message || 'Downloading update'}${amount}${percent ? ` (${percent}%)` : ''}`;
        if (current.state === 'completed') {
          fill.style.width = '100%'; status.textContent = 'Update is ready. ClipFinder will close, install the update, then reopen.';
          install.disabled = false; install.textContent = 'Restart and install update'; install.onclick = installAutomaticUpdate; return;
        }
        if (current.state === 'failed') { install.disabled = false; status.textContent = `Update download failed: ${current.message}`; return; }
        window.setTimeout(poll, 700);
      } catch (error) { install.disabled = false; status.textContent = `Update download failed: ${error.message}`; }
    };
    poll();
  } catch (error) { install.disabled = false; status.textContent = `Could not start the update: ${error.message}`; }
}

async function installAutomaticUpdate() {
  const status = $('#update-status'); const install = $('#install-update');
  try {
    install.disabled = true; status.textContent = 'Closing ClipFinder and installing the update...';
    await api(`/updates/downloads/${state.updateDownloadId}/install`, { method:'POST' });
  } catch (error) { install.disabled = false; status.textContent = `Could not install the update: ${error.message}`; }
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
    state.globalExport = { ...defaults, audio_track: Number(defaults.audio_track) };
    $('#global-layout').value = state.globalExport.layout;
    $('#global-audio-track').value = String(state.globalExport.audio_track);
    drawLayoutOverlay();
  }
}

async function loadLayoutPresets() {
  const presets = await api('/layout-presets'); const box = $('#layout-presets'); box.replaceChildren();
  for (const preset of presets) {
    const row = make('div', 'prompt-row'); row.append(make('strong', '', `${preset.name} - ${preset.layout.replace('portrait_', 'vertical ')}`));
    const use = make('button', 'quiet', 'Use'); use.onclick = async () => { try { state.globalExport = await api(`/layout-presets/${preset.id}/apply`, {method:'POST'}); state.globalExport.audio_track = Number(state.globalExport.audio_track); state.exportDirty = false; await loadExportSettings(); message(`Layout preset "${preset.name}" is active.`); } catch (error) { message(error.message, true); } };
    const remove = make('button', 'quiet', 'Delete'); remove.onclick = async () => { try { await api(`/layout-presets/${preset.id}`, {method:'DELETE'}); await loadLayoutPresets(); } catch (error) { message(error.message, true); } };
    row.append(use, remove); box.append(row);
  }
}

function calibratedRect(kind) {
  const output = state.globalExport;
  return kind === 'camera'
    ? { x:Number(output.camera_x), y:Number(output.camera_y), width:Number(output.camera_width), height:Number(output.camera_height) }
    : { x:Number(output.game_x), y:Number(output.game_y), width:Number(output.game_width), height:Number(output.game_height) };
}
function storeCalibratedRect(kind, rect) {
  if (kind === 'camera') Object.assign(state.globalExport, { camera_x:rect.x, camera_y:rect.y, camera_width:rect.width, camera_height:rect.height });
  else Object.assign(state.globalExport, { game_x:rect.x, game_y:rect.y, game_width:rect.width, game_height:rect.height });
  state.exportDirty = true;
}
function layoutCanvas() { return $('#layout-overlay'); }
function resizeLayoutCanvas() {
  const video = $('#layout-source-video'); const canvas = layoutCanvas();
  if (!video.videoWidth) return;
  const box = video.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(box.width)); canvas.height = Math.max(1, Math.round(box.height));
  canvas.style.width = `${box.width}px`; canvas.style.height = `${box.height}px`;
}
function rectanglePixels(rect, canvas) { return { x:rect.x * canvas.width, y:rect.y * canvas.height, width:rect.width * canvas.width, height:rect.height * canvas.height }; }
function drawRect(ctx, rect, color, label, canvas) {
  const box = rectanglePixels(rect, canvas); ctx.strokeStyle = color; ctx.fillStyle = `${color}22`; ctx.lineWidth = 3; ctx.fillRect(box.x, box.y, box.width, box.height); ctx.strokeRect(box.x, box.y, box.width, box.height); ctx.fillStyle = color; ctx.font = 'bold 13px system-ui'; ctx.fillText(label, box.x + 6, Math.max(16, box.y + 17));
}
function drawCroppedCover(ctx, video, rect, target) {
  const sourceX = rect.x * video.videoWidth; const sourceY = rect.y * video.videoHeight; const sourceW = rect.width * video.videoWidth; const sourceH = rect.height * video.videoHeight;
  const scale = Math.max(target.width / sourceW, target.height / sourceH); const width = sourceW * scale; const height = sourceH * scale;
  ctx.drawImage(video, sourceX, sourceY, sourceW, sourceH, target.x + (target.width - width) / 2, target.y + (target.height - height) / 2, width, height);
}
function drawCroppedContain(ctx, video, rect, target) {
  const sourceX = rect.x * video.videoWidth; const sourceY = rect.y * video.videoHeight; const sourceW = rect.width * video.videoWidth; const sourceH = rect.height * video.videoHeight;
  const scale = Math.min(target.width / sourceW, target.height / sourceH); const width = sourceW * scale; const height = sourceH * scale;
  ctx.drawImage(video, sourceX, sourceY, sourceW, sourceH, target.x + (target.width - width) / 2, target.y + (target.height - height) / 2, width, height);
}
function renderLayoutPreview() {
  const video = $('#layout-source-video'); const preview = $('#layout-output-preview'); if (!video.videoWidth) return;
  const ctx = preview.getContext('2d'); const camera = calibratedRect('camera'); const game = calibratedRect('game'); const layout = $('#global-layout').value;
  ctx.fillStyle = '#10141d'; ctx.fillRect(0, 0, preview.width, preview.height);
  try {
    if (layout === 'portrait_camera') drawCroppedCover(ctx, video, camera, {x:0,y:0,width:preview.width,height:preview.height});
    else if (layout === 'portrait_game') drawCroppedCover(ctx, video, game, {x:0,y:0,width:preview.width,height:preview.height});
    else if (layout === 'portrait_split') { drawCroppedContain(ctx, video, camera, {x:0,y:0,width:preview.width,height:preview.height / 3}); drawCroppedCover(ctx, video, game, {x:0,y:preview.height / 3,width:preview.width,height:preview.height * 2 / 3}); }
    else drawCroppedContain(ctx, video, {x:0,y:0,width:1,height:1}, {x:0,y:0,width:preview.width,height:preview.height});
  } catch { /* The video frame is simply not ready yet. */ }
}
function drawLayoutOverlay() {
  const video = $('#layout-source-video'); const canvas = layoutCanvas(); if (!video.videoWidth || !canvas.width) return;
  const ctx = canvas.getContext('2d'); ctx.clearRect(0, 0, canvas.width, canvas.height);
  drawRect(ctx, calibratedRect('camera'), '#77e3c0', 'Camera', canvas); drawRect(ctx, calibratedRect('game'), '#6db4ff', 'Gameplay', canvas);
  if (state.layoutCalibration.drawing) drawRect(ctx, state.layoutCalibration.drawing, '#ffd166', `New ${state.layoutCalibration.mode}`, canvas);
  renderLayoutPreview();
}
function canvasPoint(event) {
  const canvas = layoutCanvas(); const box = canvas.getBoundingClientRect();
  return { x:Math.max(0, Math.min(1, (event.clientX - box.left) / box.width)), y:Math.max(0, Math.min(1, (event.clientY - box.top) / box.height)) };
}
function setCalibrationMode(mode) {
  state.layoutCalibration.mode = mode; $('#layout-calibration-status').textContent = `Drawing ${mode} area: drag over the preview frame.`; layoutCanvas().classList.add('drawing');
}
function startLayoutPreview() {
  const video = state.videos.find((item) => item.id === state.videoId); if (!video) return message('Choose a recording first.', true);
  const player = $('#layout-source-video'); $('#layout-source-wrap').hidden = false; $('#layout-calibration-status').textContent = 'Pause on a representative frame, then draw the camera and gameplay areas.';
  player.src = `/api/videos/${video.id}/stream#t=1`; player.onloadedmetadata = () => { resizeLayoutCanvas(); drawLayoutOverlay(); }; player.onseeked = drawLayoutOverlay; player.ontimeupdate = drawLayoutOverlay; window.setTimeout(() => { resizeLayoutCanvas(); drawLayoutOverlay(); }, 300);
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
  for (const profile of defaults.profiles) { const option = document.createElement('option'); option.value = profile.id; option.textContent = `${profile.name} (${profile.accepted || 0} approved / ${profile.rejected || 0} rejected)`; select.append(option); }
  select.value = defaults.active_profile || previous || 'general';
  const active = defaults.profiles.find((profile) => profile.id === select.value) || {accepted:0, rejected:0};
  $('#discovery-feedback').textContent = `Learning data for this profile: ${active.accepted || 0} approved, ${active.rejected || 0} rejected. Profile-specific learning becomes stronger after several decisions in both groups.`;
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
async function refreshLibrary() { await Promise.all([loadCollections(), loadPrompts(), loadReferenceSources(), loadCaptionSettings(), loadExportSettings(), loadLayoutPresets(), loadAnalysisAudioSettings(), loadDiscoverySettings(), loadRejectionReasons()]); if (state.collectionId) await loadImportStatus(); }
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
const globalAudioPlayer = $('#global-audio-player');
globalAudioPlayer.onplay = () => { if (state.previewAudio && state.previewAudio !== globalAudioPlayer) state.previewAudio.pause(); state.previewAudio = globalAudioPlayer; };
globalAudioPlayer.onpause = () => { if (state.previewAudio === globalAudioPlayer) state.previewAudio = null; };
globalAudioPlayer.onended = () => { if (state.previewAudio === globalAudioPlayer) state.previewAudio = null; $('#audio-now-playing').hidden = true; };
globalAudioPlayer.onerror = () => { $('#audio-now-playing').hidden = true; message('Audio preview could not be played.', true); };
$('#stop-listening').onclick = () => { globalAudioPlayer.pause(); globalAudioPlayer.removeAttribute('src'); globalAudioPlayer.load(); $('#audio-now-playing').hidden = true; };
$('#listen-audio-track').onchange = () => { if (state.listeningSegment) loadListeningAudio(); };

async function uploadChatTranscript(file, delay) {
  const data = new FormData(); data.append('chat_file', file); data.append('delay_seconds', String(delay));
  const response = await fetch(`/api/videos/${state.videoId}/chat`, { method:'POST', body:data });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || 'Chat import failed.');
  return body;
}

$('#chat-import-form').onsubmit = async (event) => {
  event.preventDefault();
  if (!state.videoId) return message('Choose a recording first.', true);
  const file = $('#chat-file').files[0]; const delay = Number($('#chat-delay').value);
  if (!file) return message('Choose a chat transcript first.', true);
  if (!Number.isFinite(delay) || delay < 0 || delay > 60) return message('Enter a chat delay between 0 and 60 seconds.', true);
  const button = event.target.querySelector('button'); button.disabled = true; button.textContent = 'Importing chat...';
  try { renderChatSummary(await uploadChatTranscript(file, delay)); event.target.reset(); $('#chat-delay').value = delay; await reloadActiveSegments(); message('Chat imported and candidate scores recalculated.'); }
  catch (error) { message(error.message, true); }
  finally { button.disabled = false; button.textContent = 'Import chat and score clips'; }
};

$('#chat-delay-form').onsubmit = async (event) => {
  event.preventDefault();
  if (!state.videoId) return message('Choose a recording first.', true);
  const delay = Number($('#chat-delay').value);
  if (!Number.isFinite(delay) || delay < 0 || delay > 60) return message('Enter a chat delay between 0 and 60 seconds.', true);
  const button = event.target.querySelector('button'); button.disabled = true;
  try { renderChatSummary(await api(`/videos/${state.videoId}/chat`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({delay_seconds:delay}) })); await reloadActiveSegments(); message('Chat delay saved and candidate scores recalculated.'); }
  catch (error) { message(error.message, true); }
  finally { button.disabled = false; }
};

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
function rememberGlobalExport() { state.globalExport = { ...state.globalExport, layout: $('#global-layout').value, audio_track: Number($('#global-audio-track').value) }; state.exportDirty = true; renderLayoutPreview(); }
$('#global-layout').onchange = rememberGlobalExport;
$('#global-audio-track').onchange = rememberGlobalExport;
$('#export-defaults-form').onsubmit = async (event) => { event.preventDefault(); try { state.globalExport = await api('/export-defaults', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...state.globalExport, layout:$('#global-layout').value, audio_track:Number($('#global-audio-track').value)}) }); state.exportDirty = false; await loadExportSettings(); message('Global export settings and calibrated areas saved.'); } catch (error) { message(error.message, true); } };
$('#layout-preset-form').onsubmit = async (event) => { event.preventDefault(); const name = $('#layout-preset-name').value.trim(); if (!name) return; try { await api('/layout-presets', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...state.globalExport, name})}); event.target.reset(); await loadLayoutPresets(); message(`Layout "${name}" saved.`); } catch (error) { message(error.message, true); } };
$('#load-layout-preview').onclick = startLayoutPreview;
$('#calibrate-camera').onclick = () => setCalibrationMode('camera');
$('#calibrate-game').onclick = () => setCalibrationMode('game');
layoutCanvas().onpointerdown = (event) => { if (!$('#layout-source-video').videoWidth) return; const point = canvasPoint(event); state.layoutCalibration.start = point; state.layoutCalibration.drawing = {x:point.x, y:point.y, width:0, height:0}; layoutCanvas().setPointerCapture(event.pointerId); drawLayoutOverlay(); };
layoutCanvas().onpointermove = (event) => { const start = state.layoutCalibration.start; if (!start) return; const point = canvasPoint(event); state.layoutCalibration.drawing = {x:Math.min(start.x, point.x), y:Math.min(start.y, point.y), width:Math.abs(point.x - start.x), height:Math.abs(point.y - start.y)}; drawLayoutOverlay(); };
layoutCanvas().onpointerup = (event) => { const rect = state.layoutCalibration.drawing; if (!state.layoutCalibration.start || !rect) return; layoutCanvas().releasePointerCapture?.(event.pointerId); state.layoutCalibration.start = null; state.layoutCalibration.drawing = null; if (rect.width < .02 || rect.height < .02) { $('#layout-calibration-status').textContent = 'Area is too small. Drag a larger rectangle.'; drawLayoutOverlay(); return; } storeCalibratedRect(state.layoutCalibration.mode, rect); $('#layout-calibration-status').textContent = `${state.layoutCalibration.mode === 'camera' ? 'Camera' : 'Gameplay'} area updated. Click Save export settings to use it for exports.`; layoutCanvas().classList.remove('drawing'); drawLayoutOverlay(); };
window.addEventListener('resize', () => { resizeLayoutCanvas(); drawLayoutOverlay(); });
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
$('#score-sort').onchange = () => reloadActiveSegments();
$('#top-clips-button').onclick = async () => { if (!state.videoId) return message('Choose a recording first.', true); try { const results = await api(`/videos/${state.videoId}/top-clips?limit=10`); state.resultMode = 'top'; state.activeResults = results; await loadSegments(results); message('Showing the 10 strongest different candidates.'); } catch (error) { message(error.message, true); } };
$('#quick-review-button').onclick = openQuickReview;
$('#quick-review-close').onclick = closeQuickReview;
$('#quick-review-approve').onclick = () => rateQuickClip('accepted');
$('#quick-review-reject').onclick = () => rateQuickClip('rejected');
$('#quick-review-listen').onclick = toggleQuickListening;
$('#quick-review-previous').onclick = () => moveQuickReview(-1);
$('#quick-review-next').onclick = () => moveQuickReview(1);
$('#quick-review-dialog').addEventListener('close', () => { clearQuickReviewPreview(); globalAudioPlayer.pause(); globalAudioPlayer.removeAttribute('src'); globalAudioPlayer.load(); $('#audio-now-playing').hidden = true; });
$('#description-button').onclick = async () => { const description = $('#active-prompt').value.trim(); if (!state.videoId || description.length < 3) return message('Choose a recording and enter a prompt first.', true); try { const results = await api('/search/description', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({video_id:state.videoId, description}) }); state.resultMode = 'description'; state.activeResults = results; await loadSegments(results); } catch (error) { message(error.message, true); } };
$('#similar-button').onclick = async () => { if (!state.videoId || !state.collectionId) return message('Choose a recording and a collection first.', true); try { const results = await api(`/collections/${state.collectionId}/search`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({video_id:state.videoId})}); state.resultMode = 'similar'; state.activeResults = results; await loadSegments(results); } catch (error) { message(error.message, true); } };
$('#active-prompt').oninput = updateSelectionSummary; $('#refresh').onclick = refreshDashboard;
$('#check-updates').onclick = checkForUpdates;
function setSetupSidebar(open) {
  const sidebar = $('#setup-sidebar'); const trigger = $('#setup-toggle');
  sidebar.classList.toggle('open', open);
  document.body.classList.toggle('setup-sidebar-open', open);
  document.querySelector('main').classList.toggle('setup-sidebar-open', open);
  sidebar.setAttribute('aria-hidden', String(!open));
  trigger.setAttribute('aria-expanded', String(open));
}
$('#setup-toggle').onclick = () => setSetupSidebar(!$('#setup-sidebar').classList.contains('open'));
$('#setup-close').onclick = () => setSetupSidebar(false);
$('#clip-editor-close').onclick = () => setClipEditorOpen(false);
$('#clip-editor-toggle').onclick = () => setClipEditorOpen(true);
setClipEditorOpen(false);
document.addEventListener('keydown', (event) => {
  const quickOpen = $('#quick-review-dialog').open;
  if (quickOpen) {
    if (event.ctrlKey || event.metaKey || event.altKey || event.target.matches('input, textarea, select')) return;
    const key = event.key.toLowerCase();
    if (key === 'a') { event.preventDefault(); rateQuickClip('accepted'); }
    else if (key === 'r') { event.preventDefault(); rateQuickClip('rejected'); }
    else if (key === 's') { event.preventDefault(); toggleQuickListening(); }
    else if (event.key === 'ArrowLeft') { event.preventDefault(); moveQuickReview(-1); }
    else if (event.key === 'ArrowRight') { event.preventDefault(); moveQuickReview(1); }
    else if (event.key === 'Escape') { event.preventDefault(); closeQuickReview(); }
    return;
  }
  if (event.key === 'Escape') setSetupSidebar(false);
});
$('#close-dialog').onclick = () => { $('#full-video').pause(); $('#video-dialog').close(); };
api('/health').then(async () => { await Promise.all([refreshDashboard(), loadRuntimeStatus()]); }).catch(() => message('Local API unavailable', true)); setInterval(refreshDashboard, 4000); setInterval(() => loadRuntimeStatus().catch(() => {}), 30000);
