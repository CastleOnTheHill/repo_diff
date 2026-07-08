# repo_diff

Tool for comparing repo manifest XML files and querying Git history for
projects listed in a manifest.

## Features

- Compare two manifests and report added, removed, changed, and unchanged
  projects.
- Compare one manifest with the latest commit on each project's corresponding
  branch when the second manifest is omitted.
- For changed projects, list commits between the old and new revisions.
- Preserve full commit messages, Git notes, and trailers such as `Change-Id`.
- Generate reports as standalone HTML, Markdown, JSON, or Excel `.xlsx`.
- Filter standalone HTML reports with plain text search or regular expressions.
- Support local repo workspaces via `--repo-root`.
- Support remote mode by cloning/fetching project repositories into a cache.
- Search one manifest to check whether a commit is reachable from each
  project's manifest revision.

## Usage

Compare two manifests:

```bash
python repo_diff.py old_manifest.xml new_manifest.xml [options]
```

Compare one manifest with latest branch state:

```bash
python repo_diff.py manifest.xml [options]
```

Search one manifest for a commit:

```bash
python repo_diff.py manifest.xml --find-commit <commit-sha> [options]
```

## Options

| Option | Description |
|--------|-------------|
| `--repo-root PATH` | Repo workspace root. When set, Git history is read from local project repos. |
| `--cache-dir PATH` | Remote bare clone cache directory. Defaults to `~/.cache/repo_diff`. |
| `-o, --output FILE` | Output file path. Defaults to stdout. |
| `--format {markdown,json,html,excel}` | Output format. Defaults to `html`. |
| `--show-unchanged` | Show unchanged projects in diff mode. |
| `--allow-floating-revisions` | Allow branch names or other non-pinned revisions in diff commit ranges. |
| `--git-timeout SEC` | Timeout for each Git command. Defaults to 300 seconds. |
| `--find-commit SHA` | Search one manifest for a commit in each project history. |
| `--log-file FILE` | Write diagnostic logs for manifest parsing, remote fetches, and Git history resolution. |

## Examples

```bash
# Diff with remote fetch/cache mode and default HTML output
python repo_diff.py v1_manifest.xml v2_manifest.xml --output diff.html

# Diff one manifest against each project's latest corresponding branch
python repo_diff.py v1_manifest.xml --output latest.html

# Diff using an existing repo workspace
python repo_diff.py v1_manifest.xml v2_manifest.xml \
    --repo-root /path/to/source \
    --output diff.html

# Diff one manifest against latest branch state using an existing repo workspace
python repo_diff.py v1_manifest.xml \
    --repo-root /path/to/source \
    --output latest.html

# Markdown diff output
python repo_diff.py v1_manifest.xml v2_manifest.xml \
    --format markdown \
    --output diff.md

# JSON diff output
python repo_diff.py v1_manifest.xml v2_manifest.xml \
    --format json \
    --output diff.json

# Excel diff output
python repo_diff.py v1_manifest.xml v2_manifest.xml \
    --format excel \
    --output diff.xlsx

# Diagnose missing Git history details
python repo_diff.py v1_manifest.xml v2_manifest.xml \
    --log-file repo_diff.log \
    --output diff.html

# Increase per-command timeout for slow remote repositories
python repo_diff.py v1_manifest.xml \
    --git-timeout 900 \
    --log-file repo_diff.log \
    --output latest.html

# Search whether a commit is in each project history
python repo_diff.py manifest.xml \
    --find-commit 0123456789abcdef \
    --repo-root /path/to/source \
    --format json
```

The commit search uses:

```bash
git merge-base --is-ancestor <commit> <project-revision>
```

So `FOUND` means the commit is reachable from that project's manifest
revision, not merely present somewhere in the local object database.

When the second manifest is omitted, each project must identify a branch through
`upstream`, `dest-branch`, or a branch-valued `revision`. Pinned SHA revisions
without branch metadata are reported with an error because there is no branch to
use for the latest-state comparison. Branch-valued manifest revisions still
follow the existing `--allow-floating-revisions` safety rule.
