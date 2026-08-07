from __future__ import annotations

from pathlib import Path


APP_JS = Path(__file__).parents[1] / "app" / "static" / "app.js"


def test_dashboard_uses_adaptive_non_overlapping_polling():
    source = APP_JS.read_text(encoding="utf-8")

    assert "setInterval(() => refreshDashboard(false), 4000)" not in source
    assert "state.hasActiveVideoJobs || state.hasActiveImports ? 2000 : 15000" in source
    assert "if (state.dashboardRefreshPromise)" in source
    assert "if (!document.hidden) await refreshDashboard(false)" in source


def test_slow_dashboard_responses_cannot_replace_a_new_selection():
    source = APP_JS.read_text(encoding="utf-8")

    assert "requestedVideoId !== state.videoId || requestGeneration !== state.segmentRequestGeneration" in source
    assert "requestedVideoId !== state.videoId || requestGeneration !== state.chatRequestGeneration" in source
    assert "requestedCollectionId !== state.collectionId || requestGeneration !== state.importRequestGeneration" in source
    assert "requestedCollectionId !== state.collectionId || requestGeneration !== state.resultRequestGeneration" in source


def test_storage_and_runtime_probes_are_throttled():
    source = APP_JS.read_text(encoding="utf-8")

    assert "Date.now() - state.storageLoadedAt >= 60000" in source
    assert "function scheduleRuntimePoll(delay = 300000)" in source
    assert "captionPreviewAnimationAllowed" in source


def test_obsolete_segment_renderer_and_stuck_import_button_are_gone():
    source = APP_JS.read_text(encoding="utf-8")

    assert "legacyLoadSegments" not in source
    assert "finally { button.disabled = false; updateSelectionSummary(); }" in source


def test_media_previews_do_not_restart_or_download_twice():
    source = APP_JS.read_text(encoding="utf-8")

    assert "/audio-preview/check?audio_track=" in source
    assert "state.quickReview.previewKey === previewKey" in source
    assert "addEventListener('close', clearFullRecordingPreview)" in source


def test_sidebars_and_async_jobs_have_lifecycle_guards():
    source = APP_JS.read_text(encoding="utf-8")

    assert "sidebar.inert = !open" in source
    assert "const pollGeneration = ++state.updatePollGeneration" in source
    assert "state.collectionId !== collectionId" in source
    assert "stopLayoutPreview()" in source
