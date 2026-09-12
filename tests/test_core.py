import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from corpuspick.core import Session, digest, preview


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "corpus"
        self.root.mkdir()
        self.data = self.base / "state"
        self.session = Session(self.root, self.data)

    def tearDown(self):
        self.session.close()
        self.temp.cleanup()

    def put(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_duplicates_and_different_periods(self):
        self.put("a/отчёт.txt", "Итог за 2025: 15")
        self.put("b/copy.txt", "Итог за 2025: 15")
        self.put("c/отчёт.txt", "Итог за 2026: 17")
        docs = self.session.scan()
        self.assertEqual([d["duplicate"] > 0 for d in docs], [True, True, False])
        self.assertTrue(all(d["score"] == 1 for d in docs))

    def test_collision_origin_decision_and_reopen(self):
        for name in ("same.txt", "a/same.txt", "b/same.txt"):
            self.put(name, name)
        self.session.scan()
        relative = str(Path("a/same.txt"))
        self.session.decide(relative, 2, "Отчёт")
        self.session.flatten()
        docs = self.session.scan()
        self.assertEqual(len(list(self.root.glob("*.txt"))), 3)
        selected = next(d for d in docs if d["origin"] == relative)
        self.assertEqual(selected["score"], 2)
        self.assertEqual(self.session.remove_empty_directories(), 2)
        self.session.close()
        self.session = Session(self.root, self.data)
        self.assertTrue(self.session.undo())
        self.assertEqual(next(d for d in self.session.scan() if d["origin"] == relative)["score"], 1)

    def test_changed_source_kept(self):
        path = self.put("folder/file.txt", "before")
        self.session.scan()
        path.write_text("after")
        with self.assertRaises(ValueError):
            self.session.flatten()
        self.assertEqual(path.read_text(), "after")
        self.assertFalse((self.root / "file.txt").exists())
        self.session.cancel_pending()
        self.session.scan()
        self.session.flatten()
        self.assertEqual((self.root / "file.txt").read_text(), "after")

    def test_resume_after_link_before_unlink(self):
        source = self.put("folder/file.txt", "synthetic")
        self.session.scan()
        original_unlink = Path.unlink
        def fail(path, *args, **kwargs):
            if path.resolve() == source.resolve():
                raise OSError("simulated interruption")
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail):
            with self.assertRaises(OSError):
                self.session.flatten()
        self.assertTrue(source.exists())
        self.session.close()
        self.session = Session(self.root, self.data)
        self.session.flatten()
        self.assertFalse(source.exists())
        self.assertEqual((self.root / "file.txt").read_text(), "synthetic")

    def test_foreign_destination_never_overwritten(self):
        source = self.put("folder/file.txt", "original")
        self.session.scan()
        original_link = os.link
        def race(src, dst):
            Path(dst).write_text("other")
            return original_link(src, dst)
        with patch("os.link", race):
            with self.assertRaises(FileExistsError):
                self.session.flatten()
        with self.assertRaises(ValueError):
            self.session.flatten()
        self.assertTrue(source.exists())
        self.assertEqual((self.root / "file.txt").read_text(), "other")

    def test_readonly_scan_and_changed_decision_reset(self):
        path = self.put("a/file.txt", "one")
        before = digest(path)
        document = self.session.scan()[0]
        self.session.decide(document["path"], 2, "Письмо")
        self.assertEqual(digest(path), before)
        path.write_text("two")
        self.assertEqual(self.session.scan()[0]["score"], 1)
        self.assertEqual(self.session.remove_empty_directories(), 0)

    def test_preview_docx_and_empty(self):
        self.put("empty.txt", "")
        docx = self.root / "demo.docx"
        with ZipFile(docx, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Пример</w:t></w:r></w:p></w:body></w:document>')
        self.assertIn("Пример", preview(docx))
        self.assertEqual(next(d for d in self.session.scan() if d["path"] == "empty.txt")["score"], 0)

    def test_lock_and_state_location(self):
        with self.assertRaises(ValueError):
            Session(self.root, self.data)
        with self.assertRaises(ValueError):
            Session(self.root, self.root / "state")

    def test_path_escape(self):
        with self.assertRaises(ValueError):
            self.session.safe_path("../outside.txt")

    def test_resume_after_unlink_before_journal_save(self):
        source = self.put("folder/file.txt", "synthetic")
        self.session.scan()
        save = self.session.save
        calls = 0
        def fail_save():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("interrupted")
            save()
        with patch.object(self.session, "save", fail_save):
            with self.assertRaises(OSError):
                self.session.flatten()
        self.session.close()
        self.session = Session(self.root, self.data)
        with self.assertRaises(ValueError):
            self.session.scan()
        self.session.flatten()
        self.assertFalse(source.exists())
        self.assertEqual(self.session.scan()[0]["origin"], str(Path("folder/file.txt")))


if __name__ == "__main__":
    unittest.main()
