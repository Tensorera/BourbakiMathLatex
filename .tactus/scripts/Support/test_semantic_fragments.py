"""Fixture checks for the byte-copy and preserved include boundaries."""
import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("semantic_fragments", Path(__file__).with_name("semantic_fragments.py"))
SF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SF)


class SemanticFragmentsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.old_root, self.old_catalog = SF.ROOT, SF.CATALOG
        SF.ROOT = Path(self.directory.name)
        SF.CATALOG = SF.ROOT / "volumes.json"
        self.volume = {"order": 1, "volume": "Book", "job": "book"}
        SF.write_json(SF.CATALOG, {"volumes": [self.volume]})

    def tearDown(self):
        SF.ROOT, SF.CATALOG = self.old_root, self.old_catalog
        self.directory.cleanup()

    def book(self, main, fragments):
        for language in ("en", "zh"):
            book = SF.source_dir(self.volume, language)
            (book / "chapters").mkdir(parents=True)
            (book / "main.tex").write_bytes(main)
            (book / "main.pdf").write_bytes(b"unchanged delivery PDF\x00\x01")
            for name, data in fragments.items():
                (book / "chapters" / name).write_bytes(data)

    def plan_and_apply(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = SF.prepare(1)
        SF.write_json(SF.task_dir(self.volume) / "plan.json", {
            "schema": "semantic-fragments-plan/v1", "order": 1,
            "entries": [{"id": e["id"], "slug": e["suggested_slug"]} for e in result["entries"]]})
        with contextlib.redirect_stdout(io.StringIO()):
            verification = SF.apply(1)
        self.assertTrue(verification["ok"])
        return result

    def test_input_groups_preserve_bytes_and_cross_file_environments(self):
        main = (b"\\begin{document}\r\n\\frontmatter\n\\input{chapters/front}\n"
                b"\\mainmatter\n\\setcounter{chapter}{3}\n\\input{chapters/fragment_001}\r\n"
                b"\\input{chapters/fragment_002}\n\\end{document}\r\n")
        self.book(main, {
            "front.tex": b"\\chapter*{Preface}\r\nPreface text.\r\n",
            "fragment_001.tex": b"\\chapter{Fields}\r\n\\section{Basics}\n\\subsection{Quotients}\n\n\\begin{enumerate}\n\\item Part one",
            "fragment_002.tex": b"continued.\n\\subsection{Unsafe list heading}\n\\end{enumerate}\n\n\\subsection{Products}\nBody.\n",
            "unreferenced.tex": b"Do not delete an unrelated file.\n"})
        before = SF.load_source(SF.source_dir(self.volume, "en"))["expanded"]
        result = self.plan_and_apply()
        after = SF.load_source(SF.source_dir(self.volume, "en"))["expanded"]
        self.assertEqual(before, after)
        self.assertEqual(len(result["warnings"]["en"]), 1)
        self.assertIn("ch04-s01-ss01", [e["id"] for e in result["entries"]])
        self.assertEqual((SF.source_dir(self.volume, "en") / "chapters/unreferenced.tex").read_bytes(), b"Do not delete an unrelated file.\n")
        self.assertTrue((SF.source_dir(self.volume, "en") / "chapters/tableofcontents.json").exists())
        self.assertTrue((SF.source_dir(self.volume, "zh") / "chapters/目录.json").exists())

    def test_include_wrappers_keep_main_and_page_break_identity(self):
        main = (b"\\begin{document}\n\\frontmatter\n\\include{chapters/front}\n"
                b"\\mainmatter\n\\include{chapters/chapter}\n\\end{document}\n")
        self.book(main, {"front.tex": b"\\chapter*{Preface}\nText.\n",
                         "chapter.tex": b"\\chapter{Groups}\nIntro.\n\\section{Laws}\n\\subsection{Quotients}\nText.\n"})
        result = self.plan_and_apply()
        book = SF.source_dir(self.volume, "en")
        self.assertEqual((book / "main.tex").read_bytes(), main)
        self.assertTrue((book / "chapters/chapter.tex").read_bytes().startswith(b"\\input{"))
        toc = SF.load_json(book / "chapters/tableofcontents.json")
        self.assertEqual(len(toc["assemblies"]), 2)
        self.assertEqual(len(toc["entries"]), len(result["entries"]))
        self.assertNotIn("source_spans", toc["entries"][0])
        self.assertTrue((book / toc["provenance"]).exists())

    def test_invalid_plan_cannot_modify_books(self):
        main = b"\\begin{document}\n\\input{chapters/old}\n\\end{document}\n"
        self.book(main, {"old.tex": b"\\chapter{Groups}\nText.\n"})
        with contextlib.redirect_stdout(io.StringIO()):
            result = SF.prepare(1)
        SF.write_json(SF.task_dir(self.volume) / "plan.json", {
            "schema": "semantic-fragments-plan/v1", "order": 1,
            "entries": [{"id": e["id"], "slug": "long-slug-over-twenty-characters"} for e in result["entries"]]})
        with self.assertRaisesRegex(ValueError, "Invalid ASCII slug"):
            SF.apply(1)
        self.assertEqual((SF.source_dir(self.volume, "en") / "main.tex").read_bytes(), main)

    def test_all_structure_aliases_have_complete_arguments(self):
        data = b""
        for command, arity in SF.HEADING_ARITY.items():
            if command in {"chapter", "section", "subsection", "part"}:
                continue
            data += b"\\" + command.encode() + b"{Example}" * max(0, arity) + b"\nBody.\n"
        events, warnings = SF.scan(data)
        self.assertFalse(warnings)
        expected = len(SF.HEADING_ARITY) - 4
        self.assertEqual(len(events), expected)
        hierarchy = SF.Hierarchy()
        for event in events:
            self.assertIn("id", hierarchy.heading(event))
        summary = next(e for e in events if e["command"] == "summarychapter")
        self.assertEqual(len(summary["arguments"]), 3)

    def test_disabled_conditional_heading_is_never_a_file_boundary(self):
        events, warnings = SF.scan(b"\\chapter{Groups}\n\\iffalse\n\\section{Hidden}\n\\fi\n\\section{Visible}\n")
        self.assertEqual([e["title"] for e in events], ["Groups", "Visible"])
        self.assertEqual(warnings[0]["conditionals"], ["iffalse"])

    def test_nested_plain_inputs_are_copied_in_original_order(self):
        main = b"\\begin{document}\n\\input{chapters/wrapper}\n\\end{document}\n"
        self.book(main, {"wrapper.tex": b"\\chapter{Groups}\n\\input{chapters/body}\n",
                         "body.tex": b"\\section{Laws}\n\\subsection{Quotients}\nBody.\n"})
        before = SF.load_source(SF.source_dir(self.volume, "en"))["expanded"]
        self.plan_and_apply()
        after = SF.load_source(SF.source_dir(self.volume, "en"))["expanded"]
        self.assertEqual(before, after)

    @unittest.skipUnless(shutil.which("xelatex") and shutil.which("pdftoppm"), "XeLaTeX/Poppler required")
    def test_tex_pages_survive_eof_and_new_cut_boundaries(self):
        cases = [
            ("lf", b"Red text.\n", b"Blue text.\n", b"\n"),
            ("mixed-crlf", b"Red text.\r\n", b"Blue text.\r\n", b"\r\n"),
            ("missing-lf-mid-word", b"Red", b"Blue.\n", b"\n"),
            ("double-lf", b"Red text.\n\n", b"Blue text.\n", b"\n"),
            ("parent-blank-line", b"Red text.\n", b"Blue text.\n", b"\n\n"),
            ("comment-eof", b"Red% trailing comment\n", b"Blue text.\n", b"\n"),
            ("comment-without-lf", b"Red% trailing comment", b"Blue text.\n", b"\n"),
            ("environment-seam", b"\\begin{enumerate}\n\\item First", b"continued.\n\\item Second\n\\end{enumerate}\n", b"\n"),
            ("new-heading-cuts", b"\\section{One}\nAlpha.\n\\subsection{First}\nBeta.\n", b"Gamma.\n\\subsection{Second}\nDelta.\n", b"\n"),
        ]
        parent = SF.ROOT
        for label, a, b, gap in cases:
            with self.subTest(label=label):
                SF.ROOT = parent / label
                SF.CATALOG = SF.ROOT / "volumes.json"
                SF.write_json(SF.CATALOG, {"volumes": [self.volume]})
                main = (b"\\documentclass{article}\n\\begin{document}\n\\input{chapters/a}" + gap +
                        b"\\input{chapters/b}\n\\end{document}\n")
                self.book(main, {"a.tex": a, "b.tex": b})
                book = SF.source_dir(self.volume, "en")
                def pixels(destination):
                    target = book / destination
                    target.mkdir()
                    compiled = subprocess.run(["xelatex", "-interaction=batchmode", "-halt-on-error",
                                               f"-output-directory={destination}", "main.tex"], cwd=book,
                                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    self.assertEqual(compiled.returncode, 0, compiled.stdout.decode(errors="replace"))
                    subprocess.run(["pdftoppm", "-singlefile", "-r", "96", "-png", str(target / "main.pdf"),
                                    str(target / "page")], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    return (target / "page.png").read_bytes()
                before = pixels("before")
                self.plan_and_apply()
                after = pixels("after")
                self.assertEqual(before, after, label)


if __name__ == "__main__":
    unittest.main()
