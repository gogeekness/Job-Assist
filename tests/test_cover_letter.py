"""Cover letter: paragraph splitting, generation-time clean-up, escaping and
template rendering. unittest-style (runs under pytest too); nothing here
calls an LLM, switches the active profile, or needs a LaTeX install."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import profile as profile_mod  # noqa: E402

import generate_cv as g  # noqa: E402

SKELETON = profile_mod.SKELETON_DIR
PROFILE = {"name": "Jane Roe", "default_title": "Engineer", "location": "Berlin", "phone": "1",
           "email": "j@example.com", "nationality": "X", "visa": "", "links": [], "languages": []}


class CleanGeneratedLetter(unittest.TestCase):
    def test_strips_greeting_and_signoff(self):
        raw = "Dear Hiring Team,\n\nPara one.\n\nPara two.\n\nBest regards,\nJane Roe\n"
        self.assertEqual(g.clean_generated_letter(raw), "Para one.\n\nPara two.")

    def test_strips_german_greeting_and_signoff(self):
        raw = "Sehr geehrte Damen und Herren,\n\nErster Absatz.\n\nZweiter.\n\nMit freundlichen Grüßen\nJane Roe"
        self.assertEqual(g.clean_generated_letter(raw), "Erster Absatz.\n\nZweiter.")

    def test_strips_subject_and_labels(self):
        raw = "Subject: Application\n\nOPENING: First.\n\nBODY: Second.\n\nCLOSING: Third."
        self.assertEqual(g.clean_generated_letter(raw), "First.\n\nSecond.\n\nThird.")

    def test_keeps_sentences_that_merely_start_like_a_greeting(self):
        for first in ("Hi-performance teams are what I build.", "Dear colleagues are the reason I stayed.",
                      "Hello world was my first program, and I never stopped."):
            raw = f"{first}\n\nMiddle.\n\nEnd."
            self.assertEqual(g.clean_generated_letter(raw), f"{first}\n\nMiddle.\n\nEnd.")

    def test_keeps_sentence_starting_with_signoff_word(self):
        raw = "Intro.\n\nMiddle.\n\nSincerely interested in this role, I would welcome a call."
        self.assertEqual(g.clean_generated_letter(raw).split("\n\n")[-1],
                         "Sincerely interested in this role, I would welcome a call.")

    def test_keeps_thank_you_closing(self):
        raw = "Intro.\n\nMiddle.\n\nWhat is your top priority? Thank you for your time."
        self.assertTrue(g.clean_generated_letter(raw).endswith("Thank you for your time."))

    def test_single_newline_paragraphs(self):
        raw = "Intro line.\nMiddle line.\nLast line."
        self.assertEqual(g.clean_generated_letter(raw), "Intro line.\n\nMiddle line.\n\nLast line.")

    def test_empty(self):
        self.assertEqual(g.clean_generated_letter(""), "")
        self.assertEqual(g.clean_generated_letter(None), "")


class LetterParagraphs(unittest.TestCase):
    def test_never_strips_user_text(self):
        text = "Dear friends,\n\nBody.\n\nBest regards,\nJane"
        self.assertEqual(g.letter_paragraphs(text), ["Dear friends,", "Body.", "Best regards, Jane"])

    def test_blocks(self):
        self.assertEqual(g.letter_blocks("A.\n\nB.\n\nC.\n\nD."),
                         {"intro": "A.", "body": "B.\n\nC.", "conclusion": "D."})
        self.assertEqual(g.letter_blocks("A.\n\nB."), {"intro": "A.", "body": "", "conclusion": "B."})
        self.assertEqual(g.letter_blocks("A."), {"intro": "A.", "body": "", "conclusion": ""})
        self.assertEqual(g.letter_blocks(""), {"intro": "", "body": "", "conclusion": ""})

    def test_windows_newlines(self):
        self.assertEqual(g.letter_paragraphs("A.\r\n\r\nB."), ["A.", "B."])


class LatexEscape(unittest.TestCase):
    def test_specials(self):
        self.assertEqual(g.latex_escape("50% & $5 #1 a_b"), r"50\% \& \$5 \#1 a\_b")

    def test_invisible_and_odd_spaces(self):
        self.assertEqual(g.latex_escape("a​b﻿c­d"), "abcd")
        self.assertEqual(g.latex_escape("a b c"), "a b c")

    def test_umlauts_kept(self):
        self.assertEqual(g.latex_escape("Grüße für Öl"), "Grüße für Öl")


class Render(unittest.TestCase):
    def _render(self, template, job, letter):
        with mock.patch.dict(os.environ, {"JOB_ASSIST_PROFILE": "_skeleton"}), \
                mock.patch.object(profile_mod, "cover_letter_template_path", return_value=template):
            return g.render_cover_letter_tex(job, PROFILE, letter)

    def test_fills_blocks_in_order(self):
        tex = self._render(SKELETON / "cover_letter.tex.jinja",
                           {"company": "Acme & Co", "title": "Linux Engineer (m/w/d)"}, "Intro.\n\nBody.\n\nEnd.")
        self.assertIn(r"Dear Acme \& Co Hiring Team,", tex)
        self.assertLess(tex.index("Intro."), tex.index("Body."))
        self.assertLess(tex.index("Body."), tex.index("End."))
        self.assertIn("Linux Engineer", tex)
        self.assertNotIn("(m/w/d)", tex)

    def test_missing_company_gets_generic_greeting(self):
        tex = self._render(SKELETON / "cover_letter.tex.jinja", {"title": "T"}, "A.\n\nB.")
        self.assertIn("Dear Hiring Team,", tex)
        tex = self._render(SKELETON / "cover_letter.de.tex.jinja", {"title": "T", "company": ""}, "A.\n\nB.")
        self.assertIn("Sehr geehrte Damen und Herren,", tex)

    def test_german_skeleton(self):
        tex = self._render(SKELETON / "cover_letter.de.tex.jinja", {"company": "Müller GmbH", "title": "T"}, "A.\n\nB.")
        self.assertIn("Sehr geehrtes Team von Müller GmbH,", tex)
        self.assertIn("Mit freundlichen Grüßen", tex)

    def test_special_characters_in_text_are_escaped(self):
        tex = self._render(SKELETON / "cover_letter.tex.jinja", {"company": "X", "title": "T"}, "100% & more.\n\nEnd.")
        self.assertIn(r"100\% \& more.", tex)


class SkeletonSelection(unittest.TestCase):
    def test_language_choice(self):
        self.assertEqual(profile_mod.skeleton_cover_letter_path("de").name, "cover_letter.de.tex.jinja")
        self.assertEqual(profile_mod.skeleton_cover_letter_path("en").name, "cover_letter.tex.jinja")
        self.assertEqual(profile_mod.skeleton_cover_letter_path("fr").name, "cover_letter.tex.jinja")

    def test_legacy_mode_falls_back_to_skeleton(self):
        with mock.patch.object(profile_mod, "active_profile_dir", return_value=None):
            self.assertTrue(profile_mod.cover_letter_template_path().exists())


if __name__ == "__main__":
    unittest.main()
