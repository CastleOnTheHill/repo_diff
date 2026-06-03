"""Report generators for manifest diff results."""

import json
from typing import Dict, List

from diff_engine import ChangedProject, DiffResult


class ReportGenerator:
    """Generate formatted reports from diff results."""

    def generate(self, result: DiffResult, format: str = "markdown") -> str:
        if format == "json":
            return self._generate_json(result)
        return self._generate_markdown(result)

    def generate_commit_search(
        self,
        commit: str,
        results: List[Dict],
        format: str = "markdown",
    ) -> str:
        if format == "json":
            return json.dumps({
                "commit": commit,
                "summary": _commit_search_summary(results),
                "results": results,
            }, indent=2, ensure_ascii=False)
        return self._generate_commit_search_markdown(commit, results)

    def _generate_markdown(self, result: DiffResult) -> str:
        lines: List[str] = []
        lines.append("# Manifest Diff Report")
        lines.append("")

        summary = result.summary()
        lines.append("## Summary")
        lines.append("")
        lines.append("| Metric | Count |")
        lines.append("|--------|-------|")
        lines.append(f"| Added projects | {summary['added']} |")
        lines.append(f"| Removed projects | {summary['removed']} |")
        lines.append(f"| Changed projects | {summary['changed']} |")
        lines.append(f"| Unchanged projects | {summary['unchanged']} |")
        lines.append("")

        if result.added:
            lines.append("## Added Projects")
            lines.append("")
            lines.append("| Project | Path | Revision | Commit detail |")
            lines.append("|---------|------|----------|---------------|")
            for p in result.added:
                lines.append(
                    f"| {p.name} | {p.path} | {p.revision} | "
                    "New project; no old revision in manifests to derive per-commit changes. |"
                )
            lines.append("")

        if result.removed:
            lines.append("## Removed Projects")
            lines.append("")
            lines.append("| Project | Path | Revision | Commit detail |")
            lines.append("|---------|------|----------|---------------|")
            for p in result.removed:
                lines.append(
                    f"| {p.name} | {p.path} | {p.revision} | "
                    "Removed project; no new revision in manifests to derive per-commit changes. |"
                )
            lines.append("")

        if result.changed:
            lines.append("## Changed Projects")
            lines.append("")
            for cp in result.changed:
                lines.extend(self._format_changed_project(cp))
            lines.append("")

        if result.unchanged:
            lines.append("## Unchanged Projects")
            lines.append("")
            lines.append("| Project | Path | Revision |")
            lines.append("|---------|------|----------|")
            for p in result.unchanged:
                lines.append(f"| {p.name} | {p.path} | {p.revision} |")
            lines.append("")

        return "\n".join(lines)

    def _format_changed_project(self, cp: ChangedProject) -> List[str]:
        lines: List[str] = []
        old = cp.old
        new = cp.new
        branch = new.branch_name() or old.branch_name() or "N/A"

        lines.append(f"### {new.name}")
        lines.append("")
        lines.append(f"- **Path**: {new.path}")
        lines.append(f"- **Branch**: {branch}")
        lines.append(f"- **Revision**: `{old.revision}` -> `{new.revision}`")

        if cp.git_error:
            lines.append(f"- **Commit detail unavailable**: {cp.git_error}")
        elif cp.commits:
            lines.append("- **New commits**:")
            lines.append("")
            for c in cp.commits:
                sha_short = c["sha"][:8] if len(c["sha"]) >= 8 else c["sha"]
                lines.append(f"  - `{sha_short}` {c['author']} {c['date']}")
                lines.append("")
                lines.append("    **Message**")
                lines.extend(_indented_fenced_block(c.get("message") or c.get("subject") or ""))
                if c.get("notes"):
                    lines.append("")
                    lines.append("    **Notes**")
                    lines.extend(_indented_fenced_block(c["notes"]))
                if c.get("trailers") and c["trailers"] not in (c.get("message") or ""):
                    lines.append("")
                    lines.append("    **Trailers**")
                    lines.extend(_indented_fenced_block(c["trailers"]))
                lines.append("")
        else:
            lines.append("- **New commits**: none")
        lines.append("")
        return lines

    def _generate_json(self, result: DiffResult) -> str:
        data: Dict = {
            "summary": result.summary(),
            "added": [
                {
                    "name": p.name,
                    "path": p.path,
                    "revision": p.revision,
                    "url": p.url,
                    "url_error": p.url_error,
                    "commit_detail_status": "no_old_revision",
                }
                for p in result.added
            ],
            "removed": [
                {
                    "name": p.name,
                    "path": p.path,
                    "revision": p.revision,
                    "url": p.url,
                    "url_error": p.url_error,
                    "commit_detail_status": "no_new_revision",
                }
                for p in result.removed
            ],
            "changed": [
                {
                    "name": cp.new.name,
                    "path": cp.new.path,
                    "branch": cp.new.branch_name() or cp.old.branch_name(),
                    "old_revision": cp.old.revision,
                    "new_revision": cp.new.revision,
                    "url": cp.new.url,
                    "url_error": cp.new.url_error,
                    "commits": cp.commits,
                    "git_error": cp.git_error,
                }
                for cp in result.changed
            ],
            "unchanged": [
                {
                    "name": p.name,
                    "path": p.path,
                    "revision": p.revision,
                    "url": p.url,
                    "url_error": p.url_error,
                }
                for p in result.unchanged
            ],
        }
        return json.dumps(data, indent=2, ensure_ascii=False)

    def _generate_commit_search_markdown(self, commit: str, results: List[Dict]) -> str:
        summary = _commit_search_summary(results)
        lines = [
            "# Commit History Search",
            "",
            f"- **Commit**: `{commit}`",
            f"- **Found**: {summary['found']}",
            f"- **Not found**: {summary['not_found']}",
            f"- **Errors**: {summary['errors']}",
            "",
            "| Status | Project | Path | Revision | Detail |",
            "|--------|---------|------|----------|--------|",
        ]
        for item in results:
            if item["error"]:
                status = "ERROR"
                detail = item["error"].replace("|", "\\|")
            elif item["contains"]:
                status = "FOUND"
                detail = "commit is reachable from manifest revision"
            else:
                status = "NOT_FOUND"
                detail = "commit is not reachable from manifest revision"
            lines.append(
                f"| {status} | {item['name']} | {item['path']} | "
                f"{item['revision']} | {detail} |"
            )
        return "\n".join(lines)


def _indented_fenced_block(text: str) -> List[str]:
    """Return a markdown code block that preserves spaces and newlines."""
    fence = _safe_fence(text)
    lines = [f"    {fence}"]
    lines.extend(f"    {line}" for line in text.splitlines())
    if text.endswith("\n"):
        lines.append("    ")
    lines.append(f"    {fence}")
    return lines


def _safe_fence(text: str) -> str:
    longest = 0
    current = 0
    for char in text:
        if char == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return "`" * max(3, longest + 1)


def _commit_search_summary(results: List[Dict]) -> Dict[str, int]:
    return {
        "found": sum(1 for item in results if item["contains"] is True),
        "not_found": sum(1 for item in results if item["contains"] is False and not item["error"]),
        "errors": sum(1 for item in results if item["error"]),
    }
