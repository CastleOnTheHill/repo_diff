"""Git diff fetcher: supports local mode and remote (network) mode."""

import os
import re
import subprocess
import hashlib
from typing import Dict, List, Optional
from manifest_parser import Project, is_pinned_revision


class GitFetcher:
    """Fetch commit diffs between two revisions for a project."""

    def __init__(
        self,
        repo_root: Optional[str] = None,
        cache_dir: Optional[str] = None,
        allow_floating_revisions: bool = False,
    ):
        self.repo_root = repo_root
        self.cache_dir = cache_dir or os.path.expanduser("~/.cache/repo_diff")
        self.allow_floating_revisions = allow_floating_revisions

    def get_commits(self, old_proj: Project, new_proj: Project) -> tuple[List[Dict[str, str]], Optional[str]]:
        """
        Get list of commits between old_proj.revision and new_proj.revision.
        Returns (commits_list, error_message).
        """
        if not self.allow_floating_revisions:
            unpinned = []
            for rev in (old_proj.revision, new_proj.revision):
                if rev and not is_pinned_revision(rev) and rev not in unpinned:
                    unpinned.append(rev)
            if unpinned:
                return [], (
                    "Cannot determine commit changes from manifest alone because "
                    f"revision is not pinned: {', '.join(unpinned)}"
                )

        if self.repo_root:
            return self._get_commits_local(old_proj, new_proj)
        else:
            return self._get_commits_remote(old_proj, new_proj)

    def _get_commits_local(self, old_proj: Project, new_proj: Project) -> tuple[List[Dict[str, str]], Optional[str]]:
        """Local mode: run git log in the existing working tree."""
        local_path = os.path.join(self.repo_root, new_proj.path)
        if not self._is_git_work_tree(local_path):
            return [], f"Local git repo not found at: {local_path}"

        return self._run_git_log(local_path, old_proj.revision, new_proj.revision)

    def _get_commits_remote(self, old_proj: Project, new_proj: Project) -> tuple[List[Dict[str, str]], Optional[str]]:
        """Remote mode: use cached bare clone or create one."""
        cache_path, error = self._prepare_remote_repo(new_proj)
        if error:
            return [], error

        return self._run_git_log(cache_path, old_proj.revision, new_proj.revision)

    def contains_commit(self, project: Project, commit: str) -> tuple[Optional[bool], Optional[str]]:
        """
        Return whether commit is reachable from project.revision.
        Returns (None, error_message) when the check cannot be completed.
        """
        if not project.revision:
            return None, "Missing project revision for history check"

        if self.repo_root:
            repo_path = os.path.join(self.repo_root, project.path)
            if not self._is_git_work_tree(repo_path):
                return None, f"Local git repo not found at: {repo_path}"
        else:
            repo_path, error = self._prepare_remote_repo(project)
            if error:
                return None, error
            self._try_fetch_revision(repo_path, project.revision)
            self._try_fetch_revision(repo_path, commit)

        return self._contains_commit_in_repo(repo_path, commit, project.revision)

    def _prepare_remote_repo(self, project: Project) -> tuple[str, Optional[str]]:
        """Create or update a cached bare clone for a project."""
        if not project.url:
            return "", project.url_error or "Cannot determine repository URL for remote fetch"

        cache_name = self._cache_name(project.url)
        cache_path = os.path.join(self.cache_dir, cache_name)

        os.makedirs(self.cache_dir, exist_ok=True)

        if not os.path.exists(cache_path):
            success, err = self._partial_clone(project.url, cache_path)
            if not success:
                success, err = self._shallow_setup(project.url, cache_path)
                if not success:
                    return "", err
            self._fetch_notes(cache_path)
        else:
            self._fetch_cache(cache_path)

        return cache_path, None

    def _partial_clone(self, url: str, cache_path: str) -> tuple[bool, Optional[str]]:
        """Clone with --filter=blob:none for minimal download."""
        cmd = ["git", "clone", "--bare", "--filter=blob:none", url, cache_path]
        return self._run_cmd(cmd)

    def _shallow_setup(self, url: str, cache_path: str) -> tuple[bool, Optional[str]]:
        """Fallback: init bare repo and shallow fetch."""
        ok, err = self._run_cmd(["git", "init", "--bare", cache_path])
        if not ok:
            return False, err
        ok, err = self._run_cmd(["git", "-C", cache_path, "remote", "add", "origin", url])
        if not ok:
            return False, err
        ok, err = self._run_cmd(["git", "-C", cache_path, "fetch", "--depth=200", "origin"])
        return ok, err

    def _fetch_cache(self, cache_path: str) -> None:
        """Update an existing cache."""
        # Best-effort fetch, ignore errors
        subprocess.run(
            ["git", "-C", cache_path, "fetch", "origin"],
            capture_output=True,
        )
        self._fetch_notes(cache_path)

    def _fetch_notes(self, cache_path: str) -> None:
        """Best-effort fetch of Git notes refs used by review systems."""
        refspecs = [
            "+refs/notes/*:refs/notes/*",
            "+refs/notes/review:refs/notes/review",
            "+refs/notes/commits:refs/notes/commits",
        ]
        for refspec in refspecs:
            subprocess.run(
                ["git", "-C", cache_path, "fetch", "origin", refspec],
                capture_output=True,
            )

    def _run_git_log(self, repo_path: str, old_rev: str, new_rev: str) -> tuple[List[Dict[str, str]], Optional[str]]:
        """Run git log and parse output."""
        if not old_rev or not new_rev:
            return [], "Missing revision for comparison"

        # Determine the range syntax
        # If old_rev == new_rev, no diff
        if old_rev == new_rev:
            return [], None

        range_spec = f"{old_rev}..{new_rev}"

        cmd = [
            "git", "-C", repo_path,
            "log", range_spec,
            "--show-notes",
            "--show-notes=refs/notes/review",
            "--show-notes=refs/notes/commits",
            "--format=%x1e%H%x1f%s%x1f%an%x1f%ad%x1f%B%x1f%N%x1f%(trailers:unfold=false)",
            "--date=iso",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            stderr = result.stderr.strip()
            # Common issue: old_rev not ancestor of new_rev
            if "bad revision" in stderr.lower():
                # Try fetching the specific revisions if in remote mode
                if not self.repo_root:
                    self._try_fetch_revision(repo_path, old_rev)
                    self._try_fetch_revision(repo_path, new_rev)
                    self._fetch_notes(repo_path)
                    # Retry
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    if result.returncode == 0:
                        return self._parse_log(result.stdout), None
                return [], f"Git error: {stderr} (revisions may not be reachable or are on different branches)"
            return [], f"Git error: {stderr}"

        return self._parse_log(result.stdout), None

    def _try_fetch_revision(self, repo_path: str, revision: str) -> None:
        """Best-effort fetch of a specific revision."""
        if not revision:
            return
        # Try fetching as a SHA or ref
        subprocess.run(
            ["git", "-C", repo_path, "fetch", "origin", revision],
            capture_output=True,
        )
        # Also try with + prefix
        subprocess.run(
            ["git", "-C", repo_path, "fetch", "origin", f"+{revision}:{revision}"],
            capture_output=True,
        )

    def _parse_log(self, stdout: str) -> List[Dict[str, str]]:
        """Parse git log --format output."""
        commits = []
        for record in stdout.split("\x1e"):
            if not record.strip():
                continue
            parts = record.rstrip("\n").split("\x1f", 6)
            if len(parts) < 4:
                continue

            message = parts[4] if len(parts) > 4 else ""
            notes = parts[5] if len(parts) > 5 else ""
            trailers = parts[6] if len(parts) > 6 else ""
            commits.append({
                "sha": parts[0],
                "subject": parts[1],
                "message": message.rstrip("\n"),
                "author": parts[2],
                "date": parts[3],
                "notes": notes.rstrip("\n"),
                "trailers": trailers.rstrip("\n"),
            })
        return commits

    def _run_cmd(self, cmd: list[str]) -> tuple[bool, Optional[str]]:
        """Run a command and return (success, error_message)."""
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return False, result.stderr.strip()
        return True, None

    def _is_git_work_tree(self, path: str) -> bool:
        """Return True for normal repos and repo-tool worktrees with .git files."""
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _contains_commit_in_repo(
        self,
        repo_path: str,
        commit: str,
        revision: str,
    ) -> tuple[Optional[bool], Optional[str]]:
        commit_exists = subprocess.run(
            ["git", "-C", repo_path, "cat-file", "-e", f"{commit}^{{commit}}"],
            capture_output=True,
            text=True,
        )
        if commit_exists.returncode != 0:
            return False, None

        rev_exists = subprocess.run(
            ["git", "-C", repo_path, "cat-file", "-e", f"{revision}^{{commit}}"],
            capture_output=True,
            text=True,
        )
        if rev_exists.returncode != 0:
            return None, f"Revision is not a resolvable commit: {revision}"

        result = subprocess.run(
            ["git", "-C", repo_path, "merge-base", "--is-ancestor", commit, revision],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True, None
        if result.returncode == 1:
            return False, None
        return None, result.stderr.strip() or "Failed to check commit ancestry"

    @staticmethod
    def _cache_name(url: str) -> str:
        """Generate a safe cache directory name from a URL."""
        # Use hash to avoid filesystem issues with URLs
        h = hashlib.sha256(url.encode()).hexdigest()[:16]
        # Also include a sanitized name for human readability
        safe = re.sub(r'[^a-zA-Z0-9._-]', '_', url.split('/')[-1].replace('.git', ''))
        return f"{safe}_{h}.git"
