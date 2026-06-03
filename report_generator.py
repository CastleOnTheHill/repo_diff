"""Report generators for manifest diff results."""

import html
import json
from typing import Dict, List

from diff_engine import ChangedProject, DiffResult


class ReportGenerator:
    """Generate formatted reports from diff results."""

    def generate(self, result: DiffResult, format: str = "markdown") -> str:
        if format == "json":
            return self._generate_json(result)
        if format == "html":
            return self._generate_html(result)
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
        if format == "html":
            return self._generate_commit_search_html(commit, results)
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

    def _generate_html(self, result: DiffResult) -> str:
        summary = result.summary()
        body = [
            "<main>",
            "<h1>Manifest Diff Report</h1>",
            '<section class="summary">',
            "<h2>Summary</h2>",
            '<div class="summary-grid">',
            _summary_item("Added projects", summary["added"], "added"),
            _summary_item("Removed projects", summary["removed"], "removed"),
            _summary_item("Changed projects", summary["changed"], "changed"),
            _summary_item("Unchanged projects", summary["unchanged"], "unchanged"),
            "</div>",
            "</section>",
        ]

        if result.added:
            body.extend([
                "<section>",
                "<h2>Added Projects</h2>",
                _project_table(
                    result.added,
                    ["Project", "Path", "Revision", "Commit detail"],
                    lambda p: [
                        p.name,
                        p.path,
                        p.revision,
                        "New project; no old revision in manifests to derive per-commit changes.",
                    ],
                ),
                "</section>",
            ])

        if result.removed:
            body.extend([
                "<section>",
                "<h2>Removed Projects</h2>",
                _project_table(
                    result.removed,
                    ["Project", "Path", "Revision", "Commit detail"],
                    lambda p: [
                        p.name,
                        p.path,
                        p.revision,
                        "Removed project; no new revision in manifests to derive per-commit changes.",
                    ],
                ),
                "</section>",
            ])

        if result.changed:
            body.extend([
                "<section>",
                "<h2>Changed Projects</h2>",
            ])
            for cp in result.changed:
                body.append(self._format_changed_project_html(cp))
            body.append("</section>")

        if result.unchanged:
            body.extend([
                "<section>",
                "<h2>Unchanged Projects</h2>",
                _project_table(
                    result.unchanged,
                    ["Project", "Path", "Revision"],
                    lambda p: [p.name, p.path, p.revision],
                ),
                "</section>",
            ])

        body.append("</main>")
        return _html_page("Manifest Diff Report", "\n".join(body))

    def _format_changed_project_html(self, cp: ChangedProject) -> str:
        old = cp.old
        new = cp.new
        branch = new.branch_name() or old.branch_name() or "N/A"
        parts = [
            '<article class="changed-project">',
            f"<h3>{_e(new.name)}</h3>",
            '<dl class="metadata">',
            f"<div><dt>Path</dt><dd>{_e(new.path)}</dd></div>",
            f"<div><dt>Branch</dt><dd>{_e(branch)}</dd></div>",
            (
                "<div><dt>Revision</dt><dd>"
                f"<code>{_e(old.revision)}</code> -> <code>{_e(new.revision)}</code>"
                "</dd></div>"
            ),
            "</dl>",
        ]

        if cp.git_error:
            parts.append(f'<p class="error">Commit detail unavailable: {_e(cp.git_error)}</p>')
        elif cp.commits:
            parts.append("<h4>New commits</h4>")
            parts.append('<ol class="commit-list">')
            for c in cp.commits:
                sha = c.get("sha", "")
                sha_short = sha[:8] if len(sha) >= 8 else sha
                parts.extend([
                    "<li>",
                    '<div class="commit-meta">',
                    f"<code>{_e(sha_short)}</code>",
                    f"<span>{_e(c.get('author', ''))}</span>",
                    f"<span>{_e(c.get('date', ''))}</span>",
                    "</div>",
                    '<div class="commit-block">',
                    "<h5>Message</h5>",
                    _html_text_block(c.get("message") or c.get("subject") or ""),
                    "</div>",
                ])
                if c.get("notes"):
                    parts.extend([
                        '<div class="commit-block">',
                        "<h5>Notes</h5>",
                        _html_text_block(c["notes"]),
                        "</div>",
                    ])
                if c.get("trailers") and c["trailers"] not in (c.get("message") or ""):
                    parts.extend([
                        '<div class="commit-block">',
                        "<h5>Trailers</h5>",
                        _html_text_block(c["trailers"]),
                        "</div>",
                    ])
                parts.append("</li>")
            parts.append("</ol>")
        else:
            parts.append("<p>New commits: none</p>")

        parts.append("</article>")
        return "\n".join(parts)

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

    def _generate_commit_search_html(self, commit: str, results: List[Dict]) -> str:
        summary = _commit_search_summary(results)
        rows = []
        for item in results:
            if item["error"]:
                status = "ERROR"
                detail = item["error"]
            elif item["contains"]:
                status = "FOUND"
                detail = "commit is reachable from manifest revision"
            else:
                status = "NOT_FOUND"
                detail = "commit is not reachable from manifest revision"
            rows.append([
                f'<span class="status {status.lower()}">{_e(status)}</span>',
                _e(item["name"]),
                _e(item["path"]),
                f'<code>{_e(item["revision"])}</code>',
                _e(detail),
            ])

        body = [
            "<main>",
            "<h1>Commit History Search</h1>",
            '<section class="summary">',
            "<h2>Summary</h2>",
            f'<p class="commit-target">Commit <code>{_e(commit)}</code></p>',
            '<div class="summary-grid">',
            _summary_item("Found", summary["found"], "found"),
            _summary_item("Not found", summary["not_found"], "not-found"),
            _summary_item("Errors", summary["errors"], "errors"),
            "</div>",
            "</section>",
            "<section>",
            "<h2>Results</h2>",
            _html_table(["Status", "Project", "Path", "Revision", "Detail"], rows),
            "</section>",
            "</main>",
        ]
        return _html_page("Commit History Search", "\n".join(body))


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


def _html_page(title: str, body: str) -> str:
    return "\n".join([
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_e(title)}</title>",
        "<style>",
        _html_styles(),
        "</style>",
        "</head>",
        "<body>",
        body,
        "</body>",
        "</html>",
    ])


def _html_styles() -> str:
    return """
:root {
  color-scheme: light;
  --bg: #f7f8fa;
  --panel: #ffffff;
  --text: #1f2933;
  --muted: #5f6b7a;
  --border: #d7dde5;
  --added: #1f7a4d;
  --removed: #b42318;
  --changed: #8a5a00;
  --unchanged: #3d6478;
  --code-bg: #f0f3f7;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
main {
  width: min(1180px, calc(100% - 32px));
  margin: 0 auto;
  padding: 32px 0 48px;
}
h1, h2, h3, h4, h5 { line-height: 1.25; margin: 0; }
h1 { font-size: 32px; margin-bottom: 24px; }
h2 { font-size: 22px; margin-bottom: 14px; }
h3 { font-size: 18px; margin-bottom: 12px; }
h4 { font-size: 15px; margin: 16px 0 10px; }
h5 { font-size: 13px; color: var(--muted); margin: 0 0 6px; }
section {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 16px;
  padding: 18px;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
}
.summary-item {
  border: 1px solid var(--border);
  border-left: 4px solid var(--unchanged);
  border-radius: 6px;
  padding: 12px;
  background: #fbfcfe;
}
.summary-item.added, .summary-item.found { border-left-color: var(--added); }
.summary-item.removed, .summary-item.errors { border-left-color: var(--removed); }
.summary-item.changed, .summary-item.not-found { border-left-color: var(--changed); }
.summary-item strong {
  display: block;
  font-size: 24px;
}
.summary-item span {
  color: var(--muted);
}
table {
  width: 100%;
  border-collapse: collapse;
  overflow-wrap: anywhere;
}
th, td {
  border-bottom: 1px solid var(--border);
  padding: 9px 8px;
  text-align: left;
  vertical-align: top;
}
th {
  color: var(--muted);
  font-weight: 600;
  background: #fbfcfe;
}
tr:last-child td { border-bottom: 0; }
code {
  background: var(--code-bg);
  border-radius: 4px;
  padding: 1px 4px;
  font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
  font-size: 0.92em;
}
pre {
  margin: 0;
  padding: 12px;
  overflow-x: auto;
  white-space: pre-wrap;
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
}
.changed-project {
  border-top: 1px solid var(--border);
  padding-top: 16px;
  margin-top: 16px;
}
.changed-project:first-of-type {
  border-top: 0;
  padding-top: 0;
  margin-top: 0;
}
.metadata {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 10px;
  margin: 0;
}
.metadata div {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
}
dt {
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 3px;
}
dd { margin: 0; overflow-wrap: anywhere; }
.commit-list {
  margin: 0;
  padding-left: 24px;
}
.commit-list li {
  margin-bottom: 16px;
}
.commit-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
}
.commit-meta span {
  color: var(--muted);
}
.commit-block {
  margin: 10px 0;
}
.error, .status.error {
  color: var(--removed);
  font-weight: 600;
}
.status {
  font-weight: 700;
}
.status.found { color: var(--added); }
.status.not_found { color: var(--changed); }
.commit-target {
  margin: -6px 0 14px;
  color: var(--muted);
}
@media (max-width: 700px) {
  main {
    width: calc(100% - 20px);
    padding-top: 20px;
  }
  h1 { font-size: 26px; }
  section { padding: 14px; }
  table {
    display: block;
    overflow-x: auto;
    white-space: nowrap;
  }
}
""".strip()


def _summary_item(label: str, count: int, class_name: str) -> str:
    return (
        f'<div class="summary-item {_e(class_name)}">'
        f"<strong>{count}</strong>"
        f"<span>{_e(label)}</span>"
        "</div>"
    )


def _project_table(projects, headers: List[str], row_builder) -> str:
    rows = [[_e(cell) for cell in row_builder(project)] for project in projects]
    return _html_table(headers, rows)


def _html_table(headers: List[str], rows: List[List[str]]) -> str:
    header_html = "".join(f"<th>{_e(header)}</th>" for header in headers)
    body_rows = []
    for row in rows:
        body_rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    return (
        "<table>"
        "<thead><tr>"
        f"{header_html}"
        "</tr></thead>"
        "<tbody>"
        f"{''.join(body_rows)}"
        "</tbody>"
        "</table>"
    )


def _html_text_block(text: str) -> str:
    return f"<pre>{_e(text)}</pre>"


def _e(value) -> str:
    return html.escape(str(value), quote=True)
