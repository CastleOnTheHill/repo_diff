#!/usr/bin/env python3
"""Main entry point for repo manifest diff and commit search."""

import argparse
import logging
import sys
from pathlib import Path

from diff_engine import ChangedProject, DiffEngine, DiffResult
from git_fetcher import GitFetcher
from manifest_parser import ManifestParser
from report_generator import ReportGenerator


LOG = logging.getLogger(__name__)
LOG.addHandler(logging.NullHandler())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare repo manifests or search for a commit in one manifest.",
    )
    parser.add_argument("old_manifest", help="Base manifest XML file path")
    parser.add_argument(
        "new_manifest",
        nargs="?",
        help=(
            "Optional new manifest XML file path. If omitted in diff mode, compare each "
            "project with the latest commit on its corresponding branch."
        ),
    )
    parser.add_argument(
        "--repo-root",
        help="Repo workspace root. When set, project histories are read from local repos.",
    )
    parser.add_argument(
        "--cache-dir",
        help="Cache directory for remote bare clones. Defaults to ~/.cache/repo_diff.",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output file path. Defaults to stdout.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json", "html", "excel"],
        default="html",
        help="Output format. Defaults to html.",
    )
    parser.add_argument(
        "--show-unchanged",
        action="store_true",
        help="Show unchanged projects in diff mode.",
    )
    parser.add_argument(
        "--allow-floating-revisions",
        action="store_true",
        help="Allow git log between branch names or other non-pinned revisions.",
    )
    parser.add_argument(
        "--find-commit",
        metavar="SHA",
        help="Search one manifest and report whether this commit is in each project history.",
    )
    parser.add_argument(
        "--log-file",
        help="Write diagnostic logs for manifest parsing, fetches, and git log resolution.",
    )

    args = parser.parse_args()
    if args.log_file:
        try:
            _configure_logging(args.log_file)
        except OSError as e:
            print(f"Error: failed to initialize log file: {e}", file=sys.stderr)
            return 1

    LOG.info("repo_diff started")
    LOG.info(
        "mode=%s old_manifest=%s new_manifest=%s repo_root=%s cache_dir=%s format=%s "
        "allow_floating_revisions=%s",
        "commit_search" if args.find_commit else ("manifest_diff" if args.new_manifest else "latest_branch_diff"),
        args.old_manifest,
        args.new_manifest,
        args.repo_root,
        args.cache_dir,
        args.format,
        args.allow_floating_revisions,
    )

    old_path = Path(args.old_manifest)
    if not old_path.exists():
        print(f"Error: manifest file does not exist: {old_path}", file=sys.stderr)
        return 1

    if args.find_commit:
        return _run_commit_search(args, old_path)

    if not args.new_manifest:
        return _run_manifest_latest_diff(args, old_path)

    new_path = Path(args.new_manifest)
    if not new_path.exists():
        print(f"Error: new manifest file does not exist: {new_path}", file=sys.stderr)
        return 1

    return _run_manifest_diff(args, old_path, new_path)


def _run_manifest_diff(args: argparse.Namespace, old_path: Path, new_path: Path) -> int:
    manifest_parser = ManifestParser()
    try:
        old_manifest = manifest_parser.parse(str(old_path))
        new_manifest = manifest_parser.parse(str(new_path))
    except Exception as e:
        print(f"Error: failed to parse manifest: {e}", file=sys.stderr)
        return 1

    diff_engine = DiffEngine()
    diff_result = diff_engine.diff(old_manifest.projects, new_manifest.projects)
    LOG.info("diff summary: %s", diff_result.summary())

    if diff_result.changed:
        fetcher = GitFetcher(
            repo_root=args.repo_root,
            cache_dir=args.cache_dir,
            allow_floating_revisions=args.allow_floating_revisions,
        )
        for cp in diff_result.changed:
            LOG.info(
                "fetching commit detail: project=%s path=%s old_revision=%s new_revision=%s url=%s",
                cp.new.name,
                cp.new.path,
                cp.old.revision,
                cp.new.revision,
                cp.new.url,
            )
            commits, error = fetcher.get_commits(cp.old, cp.new)
            cp.commits = commits
            cp.git_error = error
            if error:
                LOG.warning("commit detail unavailable: project=%s error=%s", cp.new.name, error)
            else:
                LOG.info("commit detail resolved: project=%s commits=%d", cp.new.name, len(commits))

    if not args.show_unchanged:
        diff_result.unchanged = []

    generator = ReportGenerator()
    report = generator.generate(diff_result, format=args.format)
    _write_report(report, args.output)
    return 0


def _run_manifest_latest_diff(args: argparse.Namespace, manifest_path: Path) -> int:
    manifest_parser = ManifestParser()
    try:
        manifest = manifest_parser.parse(str(manifest_path))
    except Exception as e:
        print(f"Error: failed to parse manifest: {e}", file=sys.stderr)
        return 1

    fetcher = GitFetcher(
        repo_root=args.repo_root,
        cache_dir=args.cache_dir,
        allow_floating_revisions=args.allow_floating_revisions,
    )
    diff_result = DiffResult()

    for project in manifest.projects:
        LOG.info(
            "checking latest branch changes: project=%s path=%s revision=%s branch=%s url=%s",
            project.name,
            project.path,
            project.revision,
            project.branch_name(),
            project.url,
        )
        latest_project, commits, error = fetcher.get_commits_to_latest(project)
        if error:
            diff_result.changed.append(ChangedProject(
                old=project,
                new=latest_project,
                git_error=error,
            ))
            continue
        if latest_project.revision != project.revision or commits:
            diff_result.changed.append(ChangedProject(
                old=project,
                new=latest_project,
                commits=commits,
            ))
        else:
            diff_result.unchanged.append(project)

    diff_result.changed.sort(key=lambda c: c.new.name)
    diff_result.unchanged.sort(key=lambda p: p.name)
    LOG.info("latest branch diff summary: %s", diff_result.summary())

    if not args.show_unchanged:
        diff_result.unchanged = []

    generator = ReportGenerator()
    report = generator.generate(diff_result, format=args.format)
    _write_report(report, args.output)
    return 0


def _run_commit_search(args: argparse.Namespace, manifest_path: Path) -> int:
    manifest_parser = ManifestParser()
    try:
        manifest = manifest_parser.parse(str(manifest_path))
    except Exception as e:
        print(f"Error: failed to parse manifest: {e}", file=sys.stderr)
        return 1

    fetcher = GitFetcher(
        repo_root=args.repo_root,
        cache_dir=args.cache_dir,
        allow_floating_revisions=args.allow_floating_revisions,
    )

    results = []
    for project in manifest.projects:
        LOG.info(
            "checking commit reachability: project=%s path=%s revision=%s url=%s commit=%s",
            project.name,
            project.path,
            project.revision,
            project.url,
            args.find_commit,
        )
        contains, error = fetcher.contains_commit(project, args.find_commit)
        results.append({
            "name": project.name,
            "path": project.path,
            "revision": project.revision,
            "url": project.url,
            "url_error": project.url_error,
            "contains": contains,
            "error": error,
        })

    generator = ReportGenerator()
    report = generator.generate_commit_search(
        args.find_commit,
        results,
        format=args.format,
    )
    _write_report(report, args.output)
    return 0


def _write_report(report: str | bytes, output: str | None) -> None:
    if output:
        if isinstance(report, bytes):
            with open(output, "wb") as f:
                f.write(report)
        else:
            with open(output, "w", encoding="utf-8") as f:
                f.write(report)
        print(f"Report generated: {output}")
    elif isinstance(report, bytes):
        sys.stdout.buffer.write(report)
    else:
        print(report)


def _configure_logging(log_file: str) -> None:
    path = Path(log_file)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(path),
        filemode="w",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


if __name__ == "__main__":
    sys.exit(main())
