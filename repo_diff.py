#!/usr/bin/env python3
"""Main entry point for repo manifest diff and commit search."""

import argparse
import sys
from pathlib import Path

from diff_engine import DiffEngine
from git_fetcher import GitFetcher
from manifest_parser import ManifestParser
from report_generator import ReportGenerator


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare repo manifests or search for a commit in one manifest.",
    )
    parser.add_argument("old_manifest", help="Manifest XML file path")
    parser.add_argument("new_manifest", nargs="?", help="New manifest XML file path for diff mode")
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
        choices=["markdown", "json"],
        default="markdown",
        help="Output format. Defaults to markdown.",
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

    args = parser.parse_args()

    old_path = Path(args.old_manifest)
    if not old_path.exists():
        print(f"Error: manifest file does not exist: {old_path}", file=sys.stderr)
        return 1

    if args.find_commit:
        return _run_commit_search(args, old_path)

    if not args.new_manifest:
        print("Error: new_manifest is required unless --find-commit is used", file=sys.stderr)
        return 1

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

    if diff_result.changed:
        fetcher = GitFetcher(
            repo_root=args.repo_root,
            cache_dir=args.cache_dir,
            allow_floating_revisions=args.allow_floating_revisions,
        )
        for cp in diff_result.changed:
            commits, error = fetcher.get_commits(cp.old, cp.new)
            cp.commits = commits
            cp.git_error = error

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


def _write_report(report: str, output: str | None) -> None:
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Report generated: {output}")
    else:
        print(report)


if __name__ == "__main__":
    sys.exit(main())
