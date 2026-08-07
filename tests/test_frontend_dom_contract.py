from __future__ import annotations

from pathlib import Path
import unittest


STATIC_DIR = Path(__file__).parents[1] / "app" / "static"


class FrontendDomContractTest(unittest.TestCase):
    def test_detailed_scoring_has_the_grid_expected_by_the_renderer(self):
        markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="editor-score-grid"', markup)
        self.assertIn('id="editor-score-grid" class="segment-score-grid"', markup)

    def test_caption_preview_contains_the_cinematic_overlay(self):
        stylesheet = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        preview_rule = stylesheet.split(".caption-preview {", 1)[1].split("}", 1)[0]

        self.assertIn("position:relative", preview_rule)
        self.assertIn("isolation:isolate", preview_rule)
        self.assertIn('.caption-preview[data-variant="cinematic"]::before', stylesheet)


if __name__ == "__main__":
    unittest.main()
