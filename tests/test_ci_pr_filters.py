"""Pull-request path filters start a workflow for every change it checks, and only pull requests are filtered."""
from pathlib import Path
import re
import subprocess
import sys
import unittest

from tools.ci.rust_changes import rust_free_by_name, rust_reads

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/ci/vendor/pyyaml-6.0.3.zip"))
import yaml  # noqa: E402


def matches(pattern, path):
    """GitHub's filter glob: `*` and `?` stay within a folder, `**` crosses folders, `**/` may match none."""
    regex = ""
    for token in re.findall(r"\*\*/|\*\*|\*|\?|[^*?]+", pattern):
        regex += {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(token) or re.escape(token)
    return re.fullmatch(regex, path) is not None


def starts(patterns, path):
    """Whether a pull request changing `path` alone starts the workflow: the last matching pattern decides."""
    started = False
    for pattern in patterns:
        if matches(pattern.removeprefix("!"), path):
            started = not pattern.startswith("!")
    return started


class PullRequestFilterTests(unittest.TestCase):
    def test_checks_skips_documentation_alone_but_not_what_rust_reads(self):
        on = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)["on"]
        self.assertEqual(set(on), {"pull_request", "schedule", "workflow_dispatch"})
        self.assertNotIn("paths-ignore", on["pull_request"])
        paths = on["pull_request"]["paths"]
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True,
                                 check=True).stdout.split("\0")
        # The generated book pages a Rust test requires to be absent: adding one fails that test.
        absent = {"docs/modding/src/reference/hook-notes.md", "docs/modding/src/reference/neoforge-counterparts.md"}
        read = rust_reads([path for path in tracked if path and rust_free_by_name(path)] + sorted(absent))
        self.assertTrue(absent <= read)
        for path in sorted(read):
            with self.subTest(path=path):
                self.assertTrue(starts(paths, path))
        for path in ["crates/sunrise-world/src/lib.rs", "crates/sunrise-shaders/src/lib.rs", "launcher/src/main.rs",
                     "tools/parity.py", "tests/test_ci_runners.py", "Cargo.lock", ".github/workflows/ci.yml",
                     "mods/sunrise/build.gradle"]:
            with self.subTest(path=path):
                self.assertTrue(starts(paths, path))
        for path in ["docs/hosting.md", "docs/media/banner.webp", "README.md", "crates/sunrise-world/README.md"]:
            with self.subTest(path=path):
                self.assertFalse(starts(paths, path))

    def test_the_glob_follows_githubs_rules(self):
        self.assertTrue(matches("**/*.md", "README.md"))
        self.assertTrue(matches("**/*.md", "docs/a/b.md"))
        self.assertFalse(matches("docs/*.md", "docs/a/b.md"))
        self.assertTrue(starts(["**", "!docs/**", "docs/a.md"], "docs/a.md"))
        self.assertFalse(starts(["**", "!docs/**", "docs/a.md"], "docs/b.md"))


if __name__ == "__main__":
    unittest.main()
