import json
import shutil
import subprocess
import sys
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from git_fetcher import GitFetcher
from diff_engine import DiffEngine
from manifest_parser import ManifestParser
from report_generator import ReportGenerator

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_TMP_ROOT = REPO_ROOT / "tests" / ".tmp"
TEST_TMP_ROOT.mkdir(exist_ok=True)


class ManifestRiskTests(unittest.TestCase):
    @contextmanager
    def temp_dir(self):
        path = TEST_TMP_ROOT / f"case_{uuid.uuid4().hex}"
        path.mkdir()
        try:
            yield str(path)
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def parse_manifest(self, xml: str):
        with self.temp_dir() as tmpdir:
            path = Path(tmpdir) / "manifest.xml"
            path.write_text(xml, encoding="utf-8")
            return ManifestParser().parse(str(path))

    def test_project_name_with_git_suffix_does_not_duplicate_suffix(self):
        manifest = self.parse_manifest(
            """<?xml version="1.0"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <default remote="origin" revision="refs/tags/v1.0" />
  <project name="repo.git" path="repo" />
</manifest>
"""
        )

        self.assertEqual(manifest.projects[0].url, "https://github.com/org/repo.git")
        self.assertIsNone(manifest.projects[0].url_error)

    def test_relative_remote_fetch_is_reported_as_unresolvable(self):
        manifest = self.parse_manifest(
            """<?xml version="1.0"?>
<manifest>
  <remote name="origin" fetch="../org" />
  <default remote="origin" revision="refs/tags/v1.0" />
  <project name="repo" path="repo" />
</manifest>
"""
        )

        self.assertEqual(manifest.projects[0].url, "")
        self.assertIn("Relative remote fetch", manifest.projects[0].url_error)

    def test_floating_revision_is_rejected_by_default(self):
        old_manifest = self.parse_manifest(
            """<?xml version="1.0"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <project name="repo" path="repo" remote="origin" revision="release-a" />
</manifest>
"""
        )
        new_manifest = self.parse_manifest(
            """<?xml version="1.0"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <project name="repo" path="repo" remote="origin" revision="release-b" />
</manifest>
"""
        )

        commits, error = GitFetcher().get_commits(
            old_manifest.projects[0],
            new_manifest.projects[0],
        )

        self.assertEqual(commits, [])
        self.assertIn("revision is not pinned", error)

    def test_same_floating_revision_is_not_reported_as_unchanged(self):
        old_manifest = self.parse_manifest(
            """<?xml version="1.0"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <project name="repo" path="repo" remote="origin" revision="master" />
</manifest>
"""
        )
        new_manifest = self.parse_manifest(
            """<?xml version="1.0"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <project name="repo" path="repo" remote="origin" revision="master" />
</manifest>
"""
        )

        result = DiffEngine().diff(old_manifest.projects, new_manifest.projects)

        self.assertEqual(len(result.changed), 1)
        self.assertEqual(result.unchanged, [])

    def test_git_log_parser_preserves_multiline_messages_and_notes(self):
        stdout = (
            "\x1e"
            "abc123\x1fSubject  with  spaces\x1fAlice\x1f2026-04-30 10:00:00 +0800\x1f"
            "Subject  with  spaces\n\nBody line    with spaces\n  indented line\n\n"
            "Change-Id: Iabcdef\n\x1f"
            "Reviewed-by: Bob\nMerged-on: server/topic\n\x1f"
            "Change-Id: Iabcdef\n"
        )

        commits = GitFetcher()._parse_log(stdout)

        self.assertEqual(commits[0]["subject"], "Subject  with  spaces")
        self.assertEqual(
            commits[0]["message"],
            "Subject  with  spaces\n\nBody line    with spaces\n  indented line\n\nChange-Id: Iabcdef",
        )
        self.assertEqual(commits[0]["notes"], "Reviewed-by: Bob\nMerged-on: server/topic")
        self.assertEqual(commits[0]["trailers"], "Change-Id: Iabcdef")

    def test_markdown_report_uses_code_blocks_for_full_commit_text(self):
        result = DiffEngine().diff(
            self.parse_manifest(
                """<?xml version="1.0"?>
<manifest><project name="repo" path="repo" revision="1111111" /></manifest>
"""
            ).projects,
            self.parse_manifest(
                """<?xml version="1.0"?>
<manifest><project name="repo" path="repo" revision="2222222" /></manifest>
"""
            ).projects,
        )
        result.changed[0].commits = [{
            "sha": "2222222",
            "subject": "Subject",
            "message": "Subject\n\nBody    text\n  keep indent",
            "author": "Alice",
            "date": "2026-04-30 10:00:00 +0800",
            "notes": "Reviewed-by: Bob\nMerged-on: server/topic",
            "trailers": "Change-Id: Iabcdef",
        }]

        report = ReportGenerator().generate(result)

        self.assertIn("Body    text", report)
        self.assertIn("  keep indent", report)
        self.assertIn("Reviewed-by: Bob", report)
        self.assertIn("Merged-on: server/topic", report)
        self.assertIn("Change-Id: Iabcdef", report)
        self.assertNotIn("| SHA | Message | Author | Date |", report)

    def test_html_report_preserves_commit_text_and_escapes_content(self):
        result = DiffEngine().diff(
            self.parse_manifest(
                """<?xml version="1.0"?>
<manifest><project name="repo" path="repo" revision="1111111" /></manifest>
"""
            ).projects,
            self.parse_manifest(
                """<?xml version="1.0"?>
<manifest><project name="repo" path="repo" revision="2222222" /></manifest>
"""
            ).projects,
        )
        result.changed[0].commits = [{
            "sha": "2222222",
            "subject": "Subject",
            "message": "Subject\n\nBody    text\n  keep indent\n<script>alert(1)</script>",
            "author": "Alice & Bob",
            "date": "2026-04-30 10:00:00 +0800",
            "notes": "Reviewed-by: Bob\nMerged-on: server/topic",
            "trailers": "Change-Id: Iabcdef",
        }]

        report = ReportGenerator().generate(result, format="html")

        self.assertIn("<!doctype html>", report)
        self.assertIn("Manifest Diff Report", report)
        self.assertIn("Body    text", report)
        self.assertIn("  keep indent", report)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", report)
        self.assertNotIn("<script>alert(1)</script>", report)
        self.assertIn("Alice &amp; Bob", report)

    def test_run_git_log_reads_gerrit_review_notes(self):
        with self.temp_dir() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            self.run_git(repo, "init")
            self.run_git(repo, "config", "user.name", "Alice")
            self.run_git(repo, "config", "user.email", "alice@example.com")
            (repo / "file.txt").write_text("old\n", encoding="utf-8")
            self.run_git(repo, "add", "file.txt")
            self.run_git(repo, "commit", "-m", "old")
            old_rev = self.run_git(repo, "rev-parse", "HEAD").stdout.strip()

            (repo / "file.txt").write_text("new\n", encoding="utf-8")
            self.run_git(repo, "commit", "-am", "subject", "-m", "Change-Id: Iabcdef")
            new_rev = self.run_git(repo, "rev-parse", "HEAD").stdout.strip()
            self.run_git(
                repo,
                "notes",
                "--ref=review",
                "add",
                "-m",
                "Reviewed-by: Bob",
                "-m",
                "Merged-on: server/topic",
                new_rev,
            )

            commits, error = GitFetcher()._run_git_log(str(repo), old_rev, new_rev)

            self.assertIsNone(error)
            self.assertIn("Change-Id: Iabcdef", commits[0]["message"])
            self.assertIn("Reviewed-by: Bob", commits[0]["notes"])
            self.assertIn("Merged-on: server/topic", commits[0]["notes"])

    def test_repo_worktree_git_file_is_valid_local_repo(self):
        with self.temp_dir() as tmpdir:
            main = Path(tmpdir) / "main"
            linked = Path(tmpdir) / "linked"
            main.mkdir()
            self.run_git(main, "init")
            self.run_git(main, "config", "user.name", "Alice")
            self.run_git(main, "config", "user.email", "alice@example.com")
            (main / "file.txt").write_text("content\n", encoding="utf-8")
            self.run_git(main, "add", "file.txt")
            self.run_git(main, "commit", "-m", "initial")
            self.run_git(main, "worktree", "add", str(linked))

            self.assertTrue((linked / ".git").is_file())
            self.assertTrue(GitFetcher()._is_git_work_tree(str(linked)))

    def test_contains_commit_checks_manifest_revision_history(self):
        with self.temp_dir() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            self.run_git(repo, "init")
            self.run_git(repo, "config", "user.name", "Alice")
            self.run_git(repo, "config", "user.email", "alice@example.com")
            (repo / "file.txt").write_text("old\n", encoding="utf-8")
            self.run_git(repo, "add", "file.txt")
            self.run_git(repo, "commit", "-m", "old")
            old_rev = self.run_git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "file.txt").write_text("new\n", encoding="utf-8")
            self.run_git(repo, "commit", "-am", "new")
            new_rev = self.run_git(repo, "rev-parse", "HEAD").stdout.strip()

            project = self.parse_manifest(
                f"""<?xml version="1.0"?>
<manifest><project name="repo" path="repo" revision="{new_rev}" /></manifest>
"""
            ).projects[0]

            contains, error = GitFetcher(repo_root=str(root)).contains_commit(project, old_rev)

            self.assertIsNone(error)
            self.assertTrue(contains)

    def test_find_commit_cli_outputs_json(self):
        with self.temp_dir() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            self.run_git(repo, "init")
            self.run_git(repo, "config", "user.name", "Alice")
            self.run_git(repo, "config", "user.email", "alice@example.com")
            (repo / "file.txt").write_text("content\n", encoding="utf-8")
            self.run_git(repo, "add", "file.txt")
            self.run_git(repo, "commit", "-m", "initial")
            commit = self.run_git(repo, "rev-parse", "HEAD").stdout.strip()
            manifest = root / "manifest.xml"
            manifest.write_text(
                f"""<?xml version="1.0"?>
<manifest><project name="repo" path="repo" revision="{commit}" /></manifest>
""",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "repo_diff.py"),
                    str(manifest),
                    "--find-commit",
                    commit,
                    "--repo-root",
                    str(root),
                    "--format",
                    "json",
                ],
                capture_output=True,
                check=True,
                text=True,
            )

            data = json.loads(result.stdout)
            self.assertEqual(data["summary"]["found"], 1)
            self.assertTrue(data["results"][0]["contains"])

    def test_find_commit_cli_outputs_html(self):
        with self.temp_dir() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            self.run_git(repo, "init")
            self.run_git(repo, "config", "user.name", "Alice")
            self.run_git(repo, "config", "user.email", "alice@example.com")
            (repo / "file.txt").write_text("content\n", encoding="utf-8")
            self.run_git(repo, "add", "file.txt")
            self.run_git(repo, "commit", "-m", "initial")
            commit = self.run_git(repo, "rev-parse", "HEAD").stdout.strip()
            manifest = root / "manifest.xml"
            manifest.write_text(
                f"""<?xml version="1.0"?>
<manifest><project name="repo" path="repo" revision="{commit}" /></manifest>
""",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "repo_diff.py"),
                    str(manifest),
                    "--find-commit",
                    commit,
                    "--repo-root",
                    str(root),
                    "--format",
                    "html",
                ],
                capture_output=True,
                check=True,
                text=True,
            )

            self.assertIn("<!doctype html>", result.stdout)
            self.assertIn("Commit History Search", result.stdout)
            self.assertIn("FOUND", result.stdout)

    def test_cli_log_file_records_relative_fetch_diagnostics(self):
        with self.temp_dir() as tmpdir:
            root = Path(tmpdir)
            old_manifest = root / "old.xml"
            new_manifest = root / "new.xml"
            log_file = root / "repo_diff.log"
            old_manifest.write_text(
                """<?xml version="1.0"?>
<manifest>
  <remote name="origin" fetch="../org" />
  <project name="repo" path="repo" remote="origin" revision="1111111" />
</manifest>
""",
                encoding="utf-8",
            )
            new_manifest.write_text(
                """<?xml version="1.0"?>
<manifest>
  <remote name="origin" fetch="../org" />
  <project name="repo" path="repo" remote="origin" revision="2222222" />
</manifest>
""",
                encoding="utf-8",
            )

            subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "repo_diff.py"),
                    str(old_manifest),
                    str(new_manifest),
                    "--log-file",
                    str(log_file),
                ],
                capture_output=True,
                check=True,
                text=True,
            )

            log = log_file.read_text(encoding="utf-8")
            self.assertIn("Relative remote fetch cannot be resolved", log)
            self.assertIn("cannot prepare remote repo because URL is missing", log)

    def test_cli_log_file_records_unpinned_revision_diagnostics(self):
        with self.temp_dir() as tmpdir:
            root = Path(tmpdir)
            old_manifest = root / "old.xml"
            new_manifest = root / "new.xml"
            log_file = root / "repo_diff.log"
            old_manifest.write_text(
                """<?xml version="1.0"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <project name="repo" path="repo" remote="origin" revision="master" />
</manifest>
""",
                encoding="utf-8",
            )
            new_manifest.write_text(
                """<?xml version="1.0"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <project name="repo" path="repo" remote="origin" revision="main" />
</manifest>
""",
                encoding="utf-8",
            )

            subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "repo_diff.py"),
                    str(old_manifest),
                    str(new_manifest),
                    "--log-file",
                    str(log_file),
                ],
                capture_output=True,
                check=True,
                text=True,
            )

            log = log_file.read_text(encoding="utf-8")
            self.assertIn("revision is not pinned", log)
            self.assertIn("commit range rejected", log)

    def run_git(self, repo: Path, *args: str):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            check=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
