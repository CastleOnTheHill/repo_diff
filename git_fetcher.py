"""Git diff fetcher: supports local mode and remote (network) mode."""

import logging
import os
import re
import shlex
import subprocess
import hashlib
from typing import Dict, List, Optional
from manifest_parser import Project, is_pinned_revision


LOG = logging.getLogger(__name__)
LOG.addHandler(logging.NullHandler())


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
        LOG.debug(
            "git fetcher initialized: repo_root=%s cache_dir=%s allow_floating_revisions=%s",
            self.repo_root,
            self.cache_dir,
            self.allow_floating_revisions,
        )

    def get_commits(self, old_proj: Project, new_proj: Project) -> tuple[List[Dict[str, str]], Optional[str]]:
        """
        Get list of commits between old_proj.revision and new_proj.revision.
        Returns (commits_list, error_message).
        """
        LOG.info(
            "resolving commit range: project=%s old_revision=%s new_revision=%s "
            "old_pinned=%s new_pinned=%s mode=%s",
            new_proj.name,
            old_proj.revision,
            new_proj.revision,
            is_pinned_revision(old_proj.revision),
            is_pinned_revision(new_proj.revision),
            "local" if self.repo_root else "remote",
        )
        if not self.allow_floating_revisions:
            unpinned = []
            for rev in (old_proj.revision, new_proj.revision):
                if rev and not is_pinned_revision(rev) and rev not in unpinned:
                    unpinned.append(rev)
            if unpinned:
                LOG.warning(
                    "commit range rejected because revision is not pinned: project=%s revisions=%s",
                    new_proj.name,
                    ", ".join(unpinned),
                )
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
        LOG.debug("using local project path: project=%s path=%s", new_proj.name, local_path)
        if not self._is_git_work_tree(local_path):
            LOG.warning("local git repo not found: project=%s path=%s", new_proj.name, local_path)
            return [], f"Local git repo not found at: {local_path}"

        return self._run_git_log(local_path, old_proj.revision, new_proj.revision)

    def _get_commits_remote(self, old_proj: Project, new_proj: Project) -> tuple[List[Dict[str, str]], Optional[str]]:
        """Remote mode: use cached bare clone or create one."""
        LOG.debug("using remote project URL: project=%s url=%s", new_proj.name, new_proj.url)
        cache_path, error = self._prepare_remote_repo(new_proj)
        if error:
            LOG.warning("remote repo preparation failed: project=%s error=%s", new_proj.name, error)
            return [], error

        return self._run_git_log(cache_path, old_proj.revision, new_proj.revision)

    def contains_commit(self, project: Project, commit: str) -> tuple[Optional[bool], Optional[str]]:
        """
        Return whether commit is reachable from project.revision.
        Returns (None, error_message) when the check cannot be completed.
        """
        LOG.info(
            "checking commit containment: project=%s revision=%s commit=%s mode=%s",
            project.name,
            project.revision,
            commit,
            "local" if self.repo_root else "remote",
        )
        if not project.revision:
            LOG.warning("commit containment rejected because project revision is missing: %s", project.name)
            return None, "Missing project revision for history check"

        if self.repo_root:
            repo_path = os.path.join(self.repo_root, project.path)
            LOG.debug("using local project path for containment: project=%s path=%s", project.name, repo_path)
            if not self._is_git_work_tree(repo_path):
                LOG.warning("local git repo not found for containment: project=%s path=%s", project.name, repo_path)
                return None, f"Local git repo not found at: {repo_path}"
        else:
            repo_path, error = self._prepare_remote_repo(project)
            if error:
                LOG.warning("remote repo preparation failed for containment: project=%s error=%s", project.name, error)
                return None, error
            self._try_fetch_revision(repo_path, project.revision)
            self._try_fetch_revision(repo_path, commit)

        return self._contains_commit_in_repo(repo_path, commit, project.revision)

    def _prepare_remote_repo(self, project: Project) -> tuple[str, Optional[str]]:
        """Create or update a cached bare clone for a project."""
        if not project.url:
            LOG.warning(
                "cannot prepare remote repo because URL is missing: project=%s url_error=%s",
                project.name,
                project.url_error,
            )
            return "", project.url_error or "Cannot determine repository URL for remote fetch"

        cache_name = self._cache_name(project.url)
        cache_path = os.path.join(self.cache_dir, cache_name)
        LOG.info(
            "preparing remote repo cache: project=%s url=%s cache_path=%s",
            project.name,
            project.url,
            cache_path,
        )

        os.makedirs(self.cache_dir, exist_ok=True)

        if not os.path.exists(cache_path):
            LOG.info("remote cache miss: project=%s cache_path=%s", project.name, cache_path)
            success, err = self._partial_clone(project.url, cache_path)
            if not success:
                LOG.warning(
                    "partial clone failed; falling back to shallow fetch depth=200: "
                    "project=%s error=%s",
                    project.name,
                    err,
                )
                success, err = self._shallow_setup(project.url, cache_path)
                if not success:
                    LOG.error("shallow setup failed: project=%s error=%s", project.name, err)
                    return "", err
            self._fetch_notes(cache_path)
        else:
            LOG.info("remote cache hit: project=%s cache_path=%s", project.name, cache_path)
            self._fetch_cache(cache_path)

        return cache_path, None

    def _partial_clone(self, url: str, cache_path: str) -> tuple[bool, Optional[str]]:
        """Clone with --filter=blob:none for minimal download."""
        cmd = ["git", "clone", "--bare", "--filter=blob:none", url, cache_path]
        LOG.info("attempting partial bare clone: url=%s cache_path=%s", url, cache_path)
        return self._run_cmd(cmd)

    def _shallow_setup(self, url: str, cache_path: str) -> tuple[bool, Optional[str]]:
        """Fallback: init bare repo and shallow fetch."""
        LOG.warning("using shallow fallback with depth=200: url=%s cache_path=%s", url, cache_path)
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
        result = self._run_best_effort(["git", "-C", cache_path, "fetch", "origin"])
        if result.returncode != 0:
            LOG.warning("best-effort cache fetch failed and was ignored: cache_path=%s", cache_path)
        self._fetch_notes(cache_path)

    def _fetch_notes(self, cache_path: str) -> None:
        """Best-effort fetch of Git notes refs used by review systems."""
        refspecs = [
            "+refs/notes/*:refs/notes/*",
            "+refs/notes/review:refs/notes/review",
            "+refs/notes/commits:refs/notes/commits",
        ]
        for refspec in refspecs:
            result = self._run_best_effort(["git", "-C", cache_path, "fetch", "origin", refspec])
            if result.returncode != 0:
                LOG.debug(
                    "best-effort notes fetch failed and was ignored: cache_path=%s refspec=%s",
                    cache_path,
                    refspec,
                )

    def _run_git_log(self, repo_path: str, old_rev: str, new_rev: str) -> tuple[List[Dict[str, str]], Optional[str]]:
        """Run git log and parse output."""
        if not old_rev or not new_rev:
            LOG.warning("git log skipped because revision is missing: old=%s new=%s", old_rev, new_rev)
            return [], "Missing revision for comparison"

        # Determine the range syntax
        # If old_rev == new_rev, no diff
        if old_rev == new_rev:
            LOG.info("git log skipped because revisions are identical: revision=%s", old_rev)
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

        LOG.info("running git log: repo_path=%s range=%s", repo_path, range_spec)
        result = self._run_subprocess(cmd)

        if result.returncode != 0:
            stderr = result.stderr.strip()
            LOG.warning("git log failed: range=%s stderr=%s", range_spec, stderr)
            # Common issue: old_rev not ancestor of new_rev
            if "bad revision" in stderr.lower():
                # Try fetching the specific revisions if in remote mode
                if not self.repo_root:
                    LOG.info(
                        "git log bad revision; attempting to fetch specific revisions: old=%s new=%s",
                        old_rev,
                        new_rev,
                    )
                    self._try_fetch_revision(repo_path, old_rev)
                    self._try_fetch_revision(repo_path, new_rev)
                    self._fetch_notes(repo_path)
                    # Retry
                    result = self._run_subprocess(cmd)
                    if result.returncode == 0:
                        commits = self._parse_log(result.stdout)
                        LOG.info(
                            "git log succeeded after specific revision fetch: range=%s commits=%d",
                            range_spec,
                            len(commits),
                        )
                        return commits, None
                    LOG.warning("git log retry failed: range=%s stderr=%s", range_spec, result.stderr.strip())
                return [], f"Git error: {stderr} (revisions may not be reachable or are on different branches)"
            return [], f"Git error: {stderr}"

        commits = self._parse_log(result.stdout)
        LOG.info("git log succeeded: range=%s commits=%d", range_spec, len(commits))
        return commits, None

    def _try_fetch_revision(self, repo_path: str, revision: str) -> None:
        """Best-effort fetch of a specific revision."""
        if not revision:
            return
        # Try fetching as a SHA or ref
        result = self._run_best_effort(["git", "-C", repo_path, "fetch", "origin", revision])
        LOG.debug(
            "specific revision fetch result: repo_path=%s revision=%s returncode=%s",
            repo_path,
            revision,
            result.returncode,
        )
        # Also try with + prefix
        result = self._run_best_effort(["git", "-C", repo_path, "fetch", "origin", f"+{revision}:{revision}"])
        LOG.debug(
            "specific revision refspec fetch result: repo_path=%s revision=%s returncode=%s",
            repo_path,
            revision,
            result.returncode,
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
        result = self._run_subprocess(cmd)
        if result.returncode != 0:
            return False, result.stderr.strip()
        return True, None

    def _is_git_work_tree(self, path: str) -> bool:
        """Return True for normal repos and repo-tool worktrees with .git files."""
        result = self._run_subprocess(["git", "-C", path, "rev-parse", "--is-inside-work-tree"])
        is_work_tree = result.returncode == 0 and result.stdout.strip() == "true"
        LOG.debug("git work tree check: path=%s result=%s", path, is_work_tree)
        return is_work_tree

    def _contains_commit_in_repo(
        self,
        repo_path: str,
        commit: str,
        revision: str,
    ) -> tuple[Optional[bool], Optional[str]]:
        commit_exists = self._run_subprocess(["git", "-C", repo_path, "cat-file", "-e", f"{commit}^{{commit}}"])
        if commit_exists.returncode != 0:
            LOG.info("commit object is not present: repo_path=%s commit=%s", repo_path, commit)
            return False, None

        rev_exists = self._run_subprocess(["git", "-C", repo_path, "cat-file", "-e", f"{revision}^{{commit}}"])
        if rev_exists.returncode != 0:
            LOG.warning("revision is not a resolvable commit: repo_path=%s revision=%s", repo_path, revision)
            return None, f"Revision is not a resolvable commit: {revision}"

        result = self._run_subprocess(["git", "-C", repo_path, "merge-base", "--is-ancestor", commit, revision])
        if result.returncode == 0:
            LOG.info("commit is reachable from revision: commit=%s revision=%s", commit, revision)
            return True, None
        if result.returncode == 1:
            LOG.info("commit is not reachable from revision: commit=%s revision=%s", commit, revision)
            return False, None
        LOG.warning("commit ancestry check failed: stderr=%s", result.stderr.strip())
        return None, result.stderr.strip() or "Failed to check commit ancestry"

    def _run_best_effort(self, cmd: list[str]) -> subprocess.CompletedProcess:
        result = self._run_subprocess(cmd)
        if result.returncode != 0:
            LOG.debug("best-effort command failed: cmd=%s stderr=%s", _format_cmd(cmd), result.stderr.strip())
        return result

    def _run_subprocess(self, cmd: list[str]) -> subprocess.CompletedProcess:
        LOG.debug("running command: %s", _format_cmd(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            LOG.debug("command succeeded: %s", _format_cmd(cmd))
        else:
            LOG.warning(
                "command failed: returncode=%s cmd=%s stderr=%s",
                result.returncode,
                _format_cmd(cmd),
                result.stderr.strip(),
            )
        return result

    @staticmethod
    def _cache_name(url: str) -> str:
        """Generate a safe cache directory name from a URL."""
        # Use hash to avoid filesystem issues with URLs
        h = hashlib.sha256(url.encode()).hexdigest()[:16]
        # Also include a sanitized name for human readability
        safe = re.sub(r'[^a-zA-Z0-9._-]', '_', url.split('/')[-1].replace('.git', ''))
        return f"{safe}_{h}.git"


def _format_cmd(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)
