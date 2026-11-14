import re
import tempfile
import unittest
from pathlib import Path

from app import PAGE, editor_crop_filter, editor_quick_save_path, editor_replace_paths, editor_should_replace_source, parse_video_frame_rate, random_media_name


class EditorTests(unittest.TestCase):
    def test_random_media_name_matches_existing_download_style(self) -> None:
        self.assertRegex(random_media_name(), re.compile(r"^[A-Za-z0-9]{12}\.mp4$"))

    def test_full_frame_does_not_crop(self) -> None:
        self.assertIsNone(editor_crop_filter(0, 0, 1, 1))

    def test_custom_crop_uses_normalized_render_coordinates(self) -> None:
        value = editor_crop_filter(0.125, 0.2, 0.5, 0.6)
        self.assertIn("iw*0.50000000", value)
        self.assertIn("ih*0.60000000", value)
        self.assertIn("iw*0.12500000", value)
        self.assertIn("ih*0.20000000", value)

    def test_invalid_crop_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            editor_crop_filter(-0.1, 0, 1, 1)
        with self.assertRaises(ValueError):
            editor_crop_filter(0.7, 0, 0.4, 1)

    def test_frame_rate_is_read_from_video_stream(self) -> None:
        output = "Stream #0:0: Video: h264, yuv420p, 1920x1080, 29.97 fps, 30 tbr"
        self.assertEqual(parse_video_frame_rate(output), 29.97)

    def test_frame_rate_falls_back_safely(self) -> None:
        self.assertEqual(parse_video_frame_rate("no video metadata"), 30.0)

    def test_editor_exposes_frame_and_clip_controls(self) -> None:
        for control_id in ("previousFrame", "startClip", "playPause", "endClip", "nextFrame", "replayClip", "clipStartHandle", "clipEndHandle", "aspectSelect"):
            self.assertIn(f'id="{control_id}"', PAGE)
        self.assertNotIn('<span class="control-title">Clip range</span>', PAGE)

    def test_free_crop_exposes_photo_style_handles(self) -> None:
        self.assertIn('<option value="free">Aspect ratio</option>', PAGE)
        for handle in ("w", "e", "n", "s", "nw", "ne", "sw", "se"):
            self.assertIn(f'data-handle="{handle}"', PAGE)

    def test_horizontal_crop_preserves_full_height(self) -> None:
        value = editor_crop_filter(0.1, 0, 0.8, 1)
        self.assertIn("ih*1.00000000", value)
        self.assertIn("iw*0.80000000", value)

    def test_replace_mp4_renders_to_temp_at_chosen_destination(self) -> None:
        original = Path("C:/Videos/source.mp4")
        chosen = Path("D:/Exports/clipped.mp4")
        temporary, final = editor_replace_paths(original, chosen, "abc123")
        self.assertEqual(final, chosen)
        self.assertEqual(temporary, Path("D:/Exports/.clipped.abc123.editing.mp4"))

    def test_editor_has_folder_save_preservation_toggle(self) -> None:
        self.assertNotIn('id="saveVideo"', PAGE)
        self.assertIn('id="preserveOriginal"', PAGE)
        self.assertNotIn('id="saveVideoCopy"', PAGE)
        self.assertIn("preserveOriginal.checked ? 'copy' : 'replace'", PAGE)
        self.assertIn("const frameHeight = Math.min(430, availableWidth / frameRatio)", PAGE)

    def test_preserve_toggle_is_compact_and_in_the_toolbar(self) -> None:
        toolbar = PAGE[PAGE.index('<div class="editor-toolbar">'):PAGE.index('<div id="editorPreviewFrame"')]
        self.assertIn('title="Preserve original source video"', toolbar)
        self.assertIn('<span>Preserve source</span>', toolbar)
        self.assertNotIn('class="editor-actions"', PAGE)

    def test_aspect_dropdown_shows_the_current_ratio(self) -> None:
        self.assertNotIn('<option value="free">Free crop</option>', PAGE)
        self.assertIn("function readableAspectRatio(ratio)", PAGE)
        self.assertIn("freeOption.textContent = currentCropAspectLabel()", PAGE)

    def test_downloader_and_editor_share_one_view(self) -> None:
        self.assertIn('id="openVideoButton"', PAGE)
        self.assertIn('id="editorHome"', PAGE)
        self.assertNotIn('id="showEditor"', PAGE)
        self.assertIn("previewCard.appendChild(editorView)", PAGE)

    def test_pasted_video_can_be_registered_as_an_editor_source(self) -> None:
        self.assertIn("/api/editor/source/from-preview", PAGE)
        self.assertIn("expandPreviewEditor()", PAGE)
        self.assertIn("await loadFolders();", PAGE)

    def test_expanded_preview_always_contains_editor_controls(self) -> None:
        self.assertNotIn('id="editClipButton"', PAGE)
        self.assertNotIn('id="changeEditorSource"', PAGE)
        self.assertIn('class="editor-toolbar"', PAGE)
        self.assertNotIn('editing-only', PAGE)
        self.assertIn("loadEditorSource(data, sourceName)", PAGE)

    def test_quick_folders_export_the_active_edit(self) -> None:
        self.assertIn("startEditorExport(folderPath, chipElement)", PAGE)
        self.assertIn("folder: folderPath", PAGE)

    def test_quick_folder_generates_random_mp4_destination(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            destination = editor_quick_save_path(Path(folder))
            self.assertEqual(destination.parent, Path(folder).resolve())
            self.assertRegex(destination.name, re.compile(r"^[A-Za-z0-9]{12}\.mp4$"))

    def test_crop_preview_uses_maximum_available_vertical_height(self) -> None:
        self.assertIn("const frameHeight = Math.min(430, availableWidth / frameRatio)", PAGE)
        self.assertIn("editorPreviewFrame.style.height = `${Math.round(frameHeight)}px`", PAGE)

    def test_successful_save_closes_the_editor(self) -> None:
        self.assertIn("function closeEditorAfterSave()", PAGE)
        self.assertIn("closeEditorAfterSave();", PAGE)
        close_flow = PAGE[PAGE.index("function closeEditorAfterSave()"):PAGE.index("function pollEditorExport")]
        self.assertIn("output.classList.add('saving-away')", close_flow)
        self.assertIn("output.classList.remove('expanded')", close_flow)
        self.assertNotIn("minimizeEditor();", close_flow)

    def test_editor_status_is_not_visually_exposed(self) -> None:
        self.assertIn('.editor-status{position:absolute;width:1px;height:1px', PAGE)

    def test_preserve_toggle_maps_to_local_source_deletion_only(self) -> None:
        local = {"original_path": "C:/Videos/source.mp4"}
        cached = {"path": "C:/Cache/source.mp4"}
        self.assertTrue(editor_should_replace_source("replace", local))
        self.assertFalse(editor_should_replace_source("copy", local))
        self.assertFalse(editor_should_replace_source("replace", cached))

    def test_pasted_preview_starts_cache_before_editor_opens(self) -> None:
        render_start = PAGE.index("function renderPreview(data)")
        expand_start = PAGE.index("async function expandPreviewEditor()")
        self.assertIn("preparePreviewEditorSource(data);", PAGE[render_start:expand_start])

    def test_title_is_inline_and_clip_title_is_in_toolbar(self) -> None:
        self.assertIn('<div class="launch-row"><h1>Media Downloader</h1><div class="entry">', PAGE)
        toolbar = PAGE[PAGE.index('<div class="editor-toolbar">'):PAGE.index('<div id="editorPreviewFrame"')]
        self.assertLess(toolbar.index('id="aspectSelect"'), toolbar.index('id="editorSourceName"'))
        self.assertLess(toolbar.index('id="editorSourceName"'), toolbar.index('id="toggleCrop"'))
        self.assertIn(".editor-toolbar-title{order:initial", PAGE)

    def test_only_empty_editor_background_minimizes(self) -> None:
        self.assertIn("editorView.onclick = event =>", PAGE)
        self.assertIn("event.target === editorView || event.target === editorWorkspace", PAGE)

    def test_video_surface_toggles_playback_without_minimizing(self) -> None:
        self.assertIn("editorVideo.onclick = event =>", PAGE)
        self.assertIn("if (!cropMode) toggleEditorPlayback();", PAGE)

    def test_local_video_opens_as_the_same_compact_preview(self) -> None:
        self.assertIn("function renderLocalPreview(data)", PAGE)
        self.assertIn("renderLocalPreview(data);", PAGE)
        self.assertIn("if (!localEditorData && (!previewData || !previewData.editor_token)) return;", PAGE)

    def test_editor_minimize_uses_a_slow_transition(self) -> None:
        self.assertIn(".editor-card.editor-minimizing", PAGE)
        self.assertIn("editorView.classList.add('editor-minimizing')", PAGE)
        self.assertIn("setTimeout(finishEditorMinimize, 560)", PAGE)

    def test_local_video_auto_expands_into_the_editor(self) -> None:
        render_local = PAGE[PAGE.index("function renderLocalPreview(data)"):PAGE.index("function renderPreview(data)")]
        self.assertIn("local-thumb-video", render_local)
        self.assertIn("setTimeout(autoExpandLocal, 240)", render_local)
        self.assertIn("requestAnimationFrame(() => expandPreviewEditor())", render_local)

    def test_compact_preview_morphs_into_editor_surface(self) -> None:
        self.assertIn(".preview-card.editor-expanded", PAGE)
        self.assertIn("view-transition-name:editor-media", PAGE)
        self.assertIn("view-transition-name:editor-title", PAGE)
        self.assertIn("document.startViewTransition(update)", PAGE)


if __name__ == "__main__":
    unittest.main()
