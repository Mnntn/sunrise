"""The API's file list stands for git's diff only when it can't differ from it (tools/ci/pr_files.py)."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.ci import pr_files
from tools.ci.rust_changes import changed_entries, listed_entries

ENV = {"GITHUB_REPOSITORY": "o/r", "PR": "7", "BASE_SHA": "b" * 40, "HEAD_SHA": "h" * 40}


def api(files, head="h" * 40, base="b" * 40):
    """A stand-in for GitHub: paged /files, then /pulls/7."""
    def get(path):
        if path.startswith("pulls/7/files"):
            page = int(path.rsplit("page=", 1)[1])
            return files[(page - 1) * pr_files.PAGE:page * pr_files.PAGE]
        assert path == "pulls/7", path
        return {"head": {"sha": head}, "base": {"sha": base}}
    return get


class PrFilesTests(unittest.TestCase):
    def run_main(self, get, changed):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, dict(ENV, CHANGED_FILES=str(changed))), \
                mock.patch.object(pr_files, "get", side_effect=get), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            listing = Path(directory, "files")
            pr_files.main(str(listing))
            return out.getvalue(), listed_entries(listing) if listing.exists() else None

    def test_the_api_list_matches_gits_diff_without_renames(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.check_output(["git", "-C", directory, "-c", "user.name=t", "-c", "user.email=t@t",
                                                "-c", "commit.gpgsign=false", *args], text=True).strip()
            git("init", "-q")
            for name in ["modified.md", "deleted.md", "old.rs", "kept.txt"]:
                Path(directory, name).write_text(f"{name}\n" * 20)
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            Path(directory, "modified.md").write_text("changed\n")
            Path(directory, "deleted.md").unlink()
            Path(directory, "docs").mkdir()
            Path(directory, "old.rs").rename(Path(directory, "docs/new.rs"))
            Path(directory, "added.md").write_text("added\n")
            Path(directory, "copy.txt").write_text("kept.txt\n" * 20)
            git("add", "-A")
            git("commit", "-qm", "head")
            previous = Path.cwd()
            try:
                os.chdir(directory)
                expected = changed_entries(base, "HEAD")
            finally:
                os.chdir(previous)
        # What GitHub lists for the same change: renames folded, a copy marked as one.
        files = [{"status": "modified", "filename": "modified.md"}, {"status": "removed", "filename": "deleted.md"},
                 {"status": "renamed", "filename": "docs/new.rs", "previous_filename": "old.rs"},
                 {"status": "added", "filename": "added.md"}, {"status": "copied", "filename": "copy.txt"}]
        out, listed = self.run_main(api(files), len(files))
        self.assertEqual(out, "listed=true\n")
        self.assertEqual(sorted(listed), sorted(expected))

    def test_every_page_is_read(self):
        files = [{"status": "added", "filename": f"docs/{n}.md"} for n in range(pr_files.PAGE + 1)]
        out, listed = self.run_main(api(files), len(files))
        self.assertEqual(out, "listed=true\n")
        self.assertEqual(len(listed), pr_files.PAGE + 1)

    def test_any_doubt_falls_back_to_the_history(self):
        one = [{"status": "modified", "filename": "docs/setup.md"}]
        for name, get, changed in [
            ("a count other than the event's", api(one), 2),
            ("the endpoint's limit", api(one), pr_files.API_LIMIT),
            ("a head that moved", api(one, head="n" * 40), 1),
            ("a base that moved", api(one, base="n" * 40), 1),
            ("a status git has no exact twin for", api([{"status": "changed", "filename": "docs/a.md"}]), 1),
            ("an API error", mock.Mock(side_effect=OSError("HTTP 502")), 1),
        ]:
            with self.subTest(name):
                self.assertEqual(self.run_main(get, changed), ("listed=false\n", None))


if __name__ == "__main__":
    unittest.main()
