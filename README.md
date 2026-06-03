# repo_diff

Tool for comparing repo manifest XML files and querying Git history for
projects listed in a manifest.

## Features

- Compare two manifests and report added, removed, changed, and unchanged
  projects.
- For changed projects, list commits between the old and new revisions.
- Preserve full commit messages, Git notes, and trailers such as `Change-Id`.
- Generate reports as Markdown, JSON, or standalone HTML.
- Support local repo workspaces via `--repo-root`.
- Support remote mode by cloning/fetching project repositories into a cache.
- Search one manifest to check whether a commit is reachable from each
  project's manifest revision.

## Usage

Compare two manifests:

```bash
python repo_diff.py old_manifest.xml new_manifest.xml [options]
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
| `--format {markdown,json,html}` | Output format. Defaults to `markdown`. |
| `--show-unchanged` | Show unchanged projects in diff mode. |
| `--allow-floating-revisions` | Allow branch names or other non-pinned revisions in diff commit ranges. |
| `--find-commit SHA` | Search one manifest for a commit in each project history. |
| `--log-file FILE` | Write diagnostic logs for manifest parsing, remote fetches, and Git history resolution. |

## Examples

```bash
# Diff with remote fetch/cache mode
python repo_diff.py v1_manifest.xml v2_manifest.xml --output diff.md

# Diff using an existing repo workspace
python repo_diff.py v1_manifest.xml v2_manifest.xml \
    --repo-root /path/to/source \
    --output diff.md

# JSON diff output
python repo_diff.py v1_manifest.xml v2_manifest.xml \
    --format json \
    --output diff.json

# HTML diff output
python repo_diff.py v1_manifest.xml v2_manifest.xml \
    --format html \
    --output diff.html

# Diagnose missing Git history details
python repo_diff.py v1_manifest.xml v2_manifest.xml \
    --log-file repo_diff.log \
    --output diff.md

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
