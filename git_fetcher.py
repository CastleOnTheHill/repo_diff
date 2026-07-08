"""Git diff fetcher: supports local mode and remote (network) mode."""

import logging
import os
import re
import shlex
import subprocess
import hashlib
import sys
import time
from dataclasses import replace
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
        git_timeout: float = 300,
        show_progress: bool = False,
        progress_interval: float = 30,
    ):
        self.repo_root = repo_root
        self.cache_dir = cache_dir or os.path.expanduser("~/.cache/repo_diff")
        self.allow_floating_revisions = allow_floating_revisions
        self.git_timeout = git_timeout
        self.show_progress = show_progress
        self.progress_interval = progress_interval
        self._active_project: Optional[str] = None
        LOG.debug(
            "git fetcher initialized: repo_root=%s cache_dir=%s allow_floating_revisions=%s "
            "git_timeout=%s show_progress=%s progress_interval=%s",
            self.repo_root,
            self.cache_dir,
            self.allow_floating_revisions,
            self.git_timeout,
            self.show_progress,
            self.progress_interval,
        )

    def get_commits(self, old_proj: Project, new_proj: Project) -> tuple[List[Dict[str, str]], Optional[str]]:
        """
        Get list of commits between old_proj.revision and new_proj.revision.
        Returns (commits_list, error_message).
        """
        previous_project = self._active_project
        self._active_project = _project_progress_label(new_proj)
        self._emit_progress(f"{self._active_project} status=processing")
        try:
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
        finally:
            self._active_project = previous_project

    def get_commits_to_latest(
        self,
        project: Project,
    ) -> tuple[Project, List[Dict[str, str]], Optional[str]]:
        """
        Compare a manifest project revision with the latest commit on its branch.
        Returns (latest_project, commits_list, error_message).
        """
        previous_project = self._active_project
        self._active_project = _project_progress_label(project)
        self._emit_progress(f"{self._active_project} status=processing")
        try:
            branch = _comparison_branch(project)
            if not branch:
                LOG.warning("latest comparison rejected because branch is missing: project=%s", project.name)
                return project, [], (
                    "Cannot determine branch for latest comparison; manifest project needs "
                    "upstream, dest-branch, or a branch revision"
                )

            if not self.allow_floating_revisions and not is_pinned_revision(project.revision):
                LOG.warning(
                    "latest comparison rejected because manifest revision is not pinned: "
                    "project=%s revision=%s",
                    project.name,
                    project.revision,
                )
                return replace(project, revision=branch), [], (
                    "Cannot determine commit changes from manifest alone because "
                    f"revision is not pinned: {project.revision}"
                )

            repo_path, error = self._prepare_project_repo(project)
            if error:
                LOG.warning(
                    "project repo preparation failed for latest comparison: project=%s error=%s",
                    project.name,
                    error,
                )
                return replace(project, revision=branch), [], error

            if not self.repo_root:
                self._try_fetch_branch(repo_path, project, branch)
            latest_revision, error = self._resolve_latest_branch_revision(repo_path, project, branch)
            if error:
                LOG.warning("latest branch revision could not be resolved: project=%s error=%s", project.name, error)
                return replace(project, revision=branch), [], error

            old_commit = self._rev_parse_commit(repo_path, project.revision)
            if old_commit and old_commit == latest_revision:
                LOG.info(
                    "manifest revision already matches latest branch revision: project=%s revision=%s",
                    project.name,
                    latest_revision,
                )
                return project, [], None

            latest_project = replace(project, revision=latest_revision)
            commits, error = self._run_git_log(repo_path, project.revision, latest_revision)
            return latest_project, commits, error
        finally:
            self._active_project = previous_project

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

    def _prepare_project_repo(self, project: Project) -> tuple[str, Optional[str]]:
        """Return a local repo path or prepared remote cache path for a project."""
        if self.repo_root:
            local_path = os.path.join(self.repo_root, project.path)
            LOG.debug("using local project path for latest comparison: project=%s path=%s", project.name, local_path)
            if not self._is_git_work_tree(local_path):
                LOG.warning("local git repo not found: project=%s path=%s", project.name, local_path)
                return "", f"Local git repo not found at: {local_path}"
            return local_path, None
        return self._prepare_remote_repo(project)

    def contains_commit(self, project: Project, commit: str) -> tuple[Optional[bool], Optional[str]]:
        """
        Return whether commit is reachable from project.revision.
        Returns (None, error_message) when the check cannot be completed.
        """
        previous_project = self._active_project
        self._active_project = _project_progress_label(project)
        self._emit_progress(f"{self._active_project} status=processing")
        try:
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
        finally:
            self._active_project = previous_project

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
        if not commits:
            self._log_empty_range_diagnostics(repo_path, old_rev, new_rev)
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

    def _try_fetch_branch(self, repo_path: str, project: Project, branch: str) -> None:
        """Best-effort fetch of a branch into remote-tracking refs."""
        short = _short_branch_name(branch)
        if not short:
            return
        remote_names = self._remote_names(repo_path)
        for remote in remote_names:
            refspec = f"+refs/heads/{short}:refs/remotes/{remote}/{short}"
            result = self._run_best_effort(["git", "-C", repo_path, "fetch", remote, refspec])
            LOG.debug(
                "latest branch fetch result: repo_path=%s remote=%s branch=%s refspec=%s returncode=%s",
                repo_path,
                remote,
                branch,
                refspec,
                result.returncode,
            )

    def _resolve_latest_branch_revision(
        self,
        repo_path: str,
        project: Project,
        branch: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Resolve a branch name to the latest commit available in a repo."""
        candidates = _branch_ref_candidates(project, branch, self._remote_names(repo_path))
        LOG.debug(
            "resolving latest branch revision: project=%s branch=%s candidates=%s",
            project.name,
            branch,
            candidates,
        )
        for candidate in candidates:
            commit = self._rev_parse_commit(repo_path, candidate)
            if commit:
                LOG.info(
                    "latest branch revision resolved: project=%s branch=%s ref=%s commit=%s",
                    project.name,
                    branch,
                    candidate,
                    commit,
                )
                return commit, None
        return None, f"Cannot resolve latest branch revision for branch: {branch}"

    def _remote_names(self, repo_path: str) -> List[str]:
        result = self._run_subprocess(["git", "-C", repo_path, "remote"])
        if result.returncode != 0:
            return ["origin"]
        names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if names and "origin" not in names:
            names.insert(0, "origin")
        return names

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

    def _log_empty_range_diagnostics(self, repo_path: str, old_rev: str, new_rev: str) -> None:
        """Log why git log old..new resolved successfully but returned no commits."""
        old_commit = self._rev_parse_commit(repo_path, old_rev)
        new_commit = self._rev_parse_commit(repo_path, new_rev)
        LOG.warning(
            "git log returned zero commits for changed revisions: repo_path=%s old_revision=%s "
            "new_revision=%s old_commit=%s new_commit=%s",
            repo_path,
            old_rev,
            new_rev,
            old_commit or "<unresolved>",
            new_commit or "<unresolved>",
        )

        if old_commit and new_commit and old_commit == new_commit:
            LOG.warning(
                "empty range reason: old_revision and new_revision resolve to the same commit: %s",
                old_commit,
            )
            return

        if not old_commit or not new_commit:
            LOG.warning(
                "empty range reason: at least one revision could not be resolved as a commit "
                "even though git log exited successfully"
            )
            return

        old_is_ancestor = self._is_ancestor(repo_path, old_commit, new_commit)
        new_is_ancestor = self._is_ancestor(repo_path, new_commit, old_commit)
        forward_count = self._count_range(repo_path, old_commit, new_commit)
        reverse_count = self._count_range(repo_path, new_commit, old_commit)
        symmetric_count = self._count_symmetric_difference(repo_path, old_commit, new_commit)
        LOG.warning(
            "empty range ancestry diagnostics: old_is_ancestor_of_new=%s "
            "new_is_ancestor_of_old=%s forward_count=%s reverse_count=%s symmetric_count=%s",
            old_is_ancestor,
            new_is_ancestor,
            forward_count,
            reverse_count,
            symmetric_count,
        )

        if new_is_ancestor is True and old_is_ancestor is False:
            LOG.warning(
                "empty range reason: new_revision is an ancestor of old_revision, so old..new "
                "has no forward commits. This looks like a rollback or reversed manifest order."
            )
        elif symmetric_count and symmetric_count > 0:
            LOG.warning(
                "empty range reason: revisions are related in a non-forward way; use the ancestry "
                "diagnostics above to decide whether the manifest order is reversed or histories diverged."
            )

    def _rev_parse_commit(self, repo_path: str, revision: str) -> Optional[str]:
        result = self._run_subprocess(["git", "-C", repo_path, "rev-parse", f"{revision}^{{commit}}"])
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _is_ancestor(self, repo_path: str, ancestor: str, descendant: str) -> Optional[bool]:
        result = self._run_subprocess(["git", "-C", repo_path, "merge-base", "--is-ancestor", ancestor, descendant])
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        return None

    def _count_range(self, repo_path: str, old_rev: str, new_rev: str) -> Optional[int]:
        result = self._run_subprocess(["git", "-C", repo_path, "rev-list", "--count", f"{old_rev}..{new_rev}"])
        if result.returncode != 0:
            return None
        try:
            return int(result.stdout.strip())
        except ValueError:
            return None

    def _count_symmetric_difference(self, repo_path: str, old_rev: str, new_rev: str) -> Optional[int]:
        result = self._run_subprocess(["git", "-C", repo_path, "rev-list", "--count", f"{old_rev}...{new_rev}"])
        if result.returncode != 0:
            return None
        try:
            return int(result.stdout.strip())
        except ValueError:
            return None

    def _run_best_effort(self, cmd: list[str]) -> subprocess.CompletedProcess:
        result = self._run_subprocess(cmd)
        if result.returncode != 0:
            LOG.debug("best-effort command failed: cmd=%s stderr=%s", _format_cmd(cmd), result.stderr.strip())
        return result

    def _run_subprocess(self, cmd: list[str]) -> subprocess.CompletedProcess:
        LOG.debug("running command: %s", _format_cmd(cmd))
        env = _git_noninteractive_env()
        start = time.monotonic()
        self._emit_command_progress(cmd, "started", self.git_timeout)
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        stdout = ""
        stderr = ""
        next_progress = start + max(self.progress_interval, 0.1)
        while True:
            elapsed = time.monotonic() - start
            remaining = self.git_timeout - elapsed
            if remaining <= 0:
                process.kill()
                stdout, stderr = process.communicate()
                stderr = _timeout_stderr(cmd, self.git_timeout, stderr)
                LOG.warning("command timed out: cmd=%s timeout=%s", _format_cmd(cmd), self.git_timeout)
                return subprocess.CompletedProcess(
                    cmd,
                    124,
                    stdout=_stream_text(stdout),
                    stderr=stderr,
                )

            wait_for = remaining
            if self.show_progress:
                wait_for = min(wait_for, max(0.1, next_progress - time.monotonic()))

            try:
                stdout, stderr = process.communicate(timeout=wait_for)
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                remaining = max(0, self.git_timeout - (now - start))
                if self.show_progress and now >= next_progress:
                    self._emit_command_progress(cmd, "running", remaining)
                    next_progress = now + max(self.progress_interval, 0.1)

        result = subprocess.CompletedProcess(
            cmd,
            process.returncode,
            stdout=_stream_text(stdout),
            stderr=_stream_text(stderr),
        )
        remaining = max(0, self.git_timeout - (time.monotonic() - start))
        self._emit_command_progress(cmd, "finished", remaining)
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

    def _emit_progress(self, message: str) -> None:
        if self.show_progress:
            print(f"[repo_diff] {message}", file=sys.stderr, flush=True)

    def _emit_command_progress(self, cmd: list[str], status: str, remaining: float) -> None:
        if not self.show_progress:
            return
        project = self._active_project or "project=<unknown> path=<unknown>"
        self._emit_progress(
            f"{project} step={_command_step(cmd)} status={status} "
            f"timeout_remaining={max(0, remaining):.0f}s"
        )

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


def _command_step(cmd: list[str]) -> str:
    if not cmd:
        return "unknown"
    if cmd[0] == "git":
        if len(cmd) > 1 and cmd[1] == "-C" and len(cmd) > 3:
            return f"git {cmd[3]}"
        if len(cmd) > 1:
            return f"git {cmd[1]}"
        return "git"
    return cmd[0]


def _project_progress_label(project: Project) -> str:
    return f"project={project.name or '<unknown>'} path={project.path or '<unknown>'}"


def _git_noninteractive_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "Never")
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    return env


def _timeout_stderr(cmd: list[str], timeout: float, stderr) -> str:
    stderr_text = _stream_text(stderr)
    message = f"Command timed out after {timeout:g}s: {_format_cmd(cmd)}"
    if stderr_text.strip():
        return f"{message}\n{stderr_text.strip()}"
    return message


def _stream_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _comparison_branch(project: Project) -> Optional[str]:
    branch = project.branch_name()
    if not branch or branch.startswith("refs/tags/"):
        return None
    return branch


def _short_branch_name(branch: str) -> str:
    if branch.startswith("refs/heads/"):
        return branch[len("refs/heads/"):]
    if branch.startswith("refs/remotes/"):
        parts = branch.split("/", 3)
        return parts[3] if len(parts) == 4 else ""
    return branch


def _branch_ref_candidates(
    project: Project,
    branch: str,
    remote_names: List[str],
) -> List[str]:
    candidates: List[str] = []

    def add(value: str) -> None:
        if value and value not in candidates:
            candidates.append(value)

    short = _short_branch_name(branch)
    add(branch)
    if branch.startswith("refs/heads/"):
        add(short)
    if short:
        for remote in remote_names:
            add(f"refs/remotes/{remote}/{short}")
            add(f"{remote}/{short}")
        if project.remote and project.remote not in remote_names:
            add(f"refs/remotes/{project.remote}/{short}")
            add(f"{project.remote}/{short}")
        add(f"refs/heads/{short}")
        add(short)
    return candidates
