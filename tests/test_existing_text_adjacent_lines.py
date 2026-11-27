"""Regression tests for the separate existing-PDF-text editor."""
import unittest

import pymupdf as fitz

from app import _apply_existing_text_edits


class AdjacentLineRedactionTests(unittest.TestCase):
    def test_replacing_one_line_does_not_remove_the_next_line(self):
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text(
            (72, 200),
            "Playwright, Pytest, Page Object Model (POM), CI/CD with GitHub Actions",
            fontsize=8.64,
            fontname="helv",
        )
        page.insert_text(
            (72, 212),
            "Manual Testing: Functional, Regression, Smoke, Integration",
            fontsize=8.64,
            fontname="helv",
        )

        first_span = None
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if span["text"].startswith("Playwright"):
                        first_span = span
                        break
        self.assertIsNotNone(first_span)

        edited = _apply_existing_text_edits(
            doc.tobytes(),
            [{
                "page": 1,
                "bbox": list(first_span["bbox"]),
                "original_text": first_span["text"],
                "new_text": "Playwright, Python, Page Object Model",
                "background": "#ffffff",
                "fit_to_width": True,
            }],
        )
        result = fitz.open(stream=edited, filetype="pdf")
        extracted = result[0].get_text()

        self.assertIn("Playwright, Python, Page Object Model", extracted)
        self.assertIn(
            "Manual Testing: Functional, Regression, Smoke, Integration",
            extracted,
        )
        result.close()
        doc.close()

    def test_long_replacement_uses_free_page_width_before_shrinking(self):
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 200), "Automation Tools: Playwright, Pytest", fontsize=10, fontname="helv")
        span = page.get_text("dict")["blocks"][0]["lines"][0]["spans"][0]

        edited = _apply_existing_text_edits(
            doc.tobytes(),
            [{
                "page": 1,
                "bbox": list(span["bbox"]),
                "original_text": span["text"],
                "new_text": "Automation Tools: Playwright, Pytest, Page Object Model (POM), CI/CD with GitHub Actions",
                "background": "#ffffff",
                "fit_to_width": True,
            }],
        )
        result = fitz.open(stream=edited, filetype="pdf")
        self.assertIn("Page Object Model (POM)", result[0].get_text())
        replacement_spans = [
            span for block in result[0].get_text("dict")["blocks"] if block.get("type") == 0
            for line in block["lines"] for span in line["spans"]
            if "Automation Tools:" in span.get("text", "")
        ]
        self.assertTrue(replacement_spans)
        self.assertAlmostEqual(replacement_spans[-1]["size"], 10, delta=0.05)
        result.close()
        doc.close()

    def test_replacement_does_not_extend_over_following_same_line_text(self):
        """Blocked on the right, free below: the text wraps instead of covering KEEP THIS TEXT."""
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 200), "Automation Tools:", fontsize=10, fontname="helv")
        page.insert_text((180, 200), "KEEP THIS TEXT", fontsize=10, fontname="helv")
        first = page.get_text("dict")["blocks"][0]["lines"][0]["spans"][0]

        edited = _apply_existing_text_edits(
            doc.tobytes(),
            [{
                "page": 1,
                "bbox": list(first["bbox"]),
                "original_text": first["text"],
                "new_text": "Automation Tools: Playwright, Pytest, Page Object Model (POM), CI/CD",
                "background": "#ffffff",
                "fit_to_width": True,
            }],
        )
        result = fitz.open(stream=edited, filetype="pdf")[0]
        self.assertIn("KEEP THIS TEXT", result.get_text())
        keep = result.search_for("KEEP THIS TEXT")[0]
        for word in result.get_text("words"):
            if word[4] in ("KEEP", "THIS", "TEXT"):
                continue
            self.assertTrue((fitz.Rect(word[:4]) & keep).is_empty, word)
        doc.close()


if __name__ == "__main__":
    unittest.main()
