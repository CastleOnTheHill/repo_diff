"""Manifest XML parser for repo manifest files."""

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Dict, Optional


LOG = logging.getLogger(__name__)
LOG.addHandler(logging.NullHandler())


@dataclass
class Project:
    """Represents a single project in a repo manifest."""
    name: str
    path: str
    remote: str
    revision: str
    upstream: Optional[str] = None
    dest_branch: Optional[str] = None
    groups: Optional[str] = None
    url: str = ""
    url_error: Optional[str] = None

    def branch_name(self) -> Optional[str]:
        """Return the branch name for this project."""
        if self.upstream:
            return self.upstream
        if self.dest_branch:
            return self.dest_branch
        # If revision is not a SHA, treat it as a branch/tag name
        if self.revision and not _is_sha(self.revision):
            return self.revision
        return None


def _is_sha(value: str) -> bool:
    """Check if a string looks like a Git SHA-1 hash."""
    if not value:
        return False
    # Remove refs/heads/ or refs/tags/ prefix
    if value.startswith("refs/heads/"):
        return False
    if value.startswith("refs/tags/"):
        return False
    # SHA-1 is hex string of length 7-40
    stripped = value.strip()
    return len(stripped) >= 7 and len(stripped) <= 40 and all(c in "0123456789abcdefABCDEF" for c in stripped)


def is_pinned_revision(value: str) -> bool:
    """Return True when a manifest revision identifies a stable Git object."""
    if not value:
        return False
    stripped = value.strip()
    return _is_sha(stripped) or stripped.startswith("refs/tags/")


@dataclass
class Manifest:
    """Represents a parsed repo manifest."""
    projects: List[Project] = field(default_factory=list)


class ManifestParser:
    """Parse a repo manifest XML file into a Manifest object."""

    def parse(self, xml_path: str) -> Manifest:
        LOG.info("parsing manifest: %s", xml_path)
        tree = ET.parse(xml_path)
        root = tree.getroot()

        # Parse remotes
        remotes: Dict[str, Dict[str, str]] = {}
        for remote_elem in root.findall("remote"):
            name = remote_elem.get("name", "").strip()
            if name:
                remotes[name] = {
                    "fetch": remote_elem.get("fetch", "").strip(),
                    "revision": (remote_elem.get("revision") or "").strip(),
                    "alias": (remote_elem.get("alias") or "").strip(),
                }
                LOG.debug(
                    "manifest remote: name=%s fetch=%s revision=%s alias=%s",
                    name,
                    remotes[name]["fetch"],
                    remotes[name]["revision"],
                    remotes[name]["alias"],
                )

        # Parse default
        default = {
            "remote": "",
            "revision": "",
            "dest_branch": "",
            "upstream": "",
        }
        default_elem = root.find("default")
        if default_elem is not None:
            default["remote"] = (default_elem.get("remote") or "").strip()
            default["revision"] = (default_elem.get("revision") or "").strip()
            default["dest_branch"] = (default_elem.get("dest-branch") or "").strip()
            default["upstream"] = (default_elem.get("upstream") or "").strip()
        LOG.debug("manifest default: %s", default)

        # Parse projects
        projects: List[Project] = []
        for proj_elem in root.findall("project"):
            name = proj_elem.get("name", "").strip()
            if not name:
                continue

            # Resolve inherited attributes
            path = (proj_elem.get("path") or name).strip()
            remote_name = (proj_elem.get("remote") or default["remote"]).strip()
            revision = (proj_elem.get("revision") or "").strip()
            upstream = (proj_elem.get("upstream") or default["upstream"] or "").strip()
            dest_branch = (proj_elem.get("dest-branch") or default["dest_branch"] or "").strip()
            groups = (proj_elem.get("groups") or "").strip()

            # Revision fallback: project -> remote -> default
            if not revision:
                if remote_name in remotes and remotes[remote_name]["revision"]:
                    revision = remotes[remote_name]["revision"]
                else:
                    revision = default["revision"]

            # Build URL
            url = ""
            url_error = None
            if remote_name in remotes:
                fetch = remotes[remote_name]["fetch"]
                if fetch:
                    url, url_error = _build_project_url(fetch, name)
            else:
                LOG.warning("project remote is not defined: project=%s remote=%s", name, remote_name)
            if url_error:
                LOG.warning(
                    "project URL could not be resolved: project=%s remote=%s fetch=%s error=%s",
                    name,
                    remote_name,
                    remotes.get(remote_name, {}).get("fetch", ""),
                    url_error,
                )
            else:
                LOG.debug(
                    "project parsed: name=%s path=%s remote=%s revision=%s url=%s",
                    name,
                    path,
                    remote_name,
                    revision,
                    url,
                )

            projects.append(Project(
                name=name,
                path=path,
                remote=remote_name,
                revision=revision,
                upstream=upstream or None,
                dest_branch=dest_branch or None,
                groups=groups or None,
                url=url,
                url_error=url_error,
            ))

        # Handle remove-project elements
        removed_names = set()
        for rem_elem in root.findall("remove-project"):
            removed_name = rem_elem.get("name", "").strip()
            if removed_name:
                removed_names.add(removed_name)

        projects = [p for p in projects if p.name not in removed_names]
        if removed_names:
            LOG.info("remove-project entries applied: %s", sorted(removed_names))

        LOG.info("manifest parsed: %s projects=%d", xml_path, len(projects))
        return Manifest(projects=projects)


def _build_project_url(fetch: str, name: str) -> tuple[str, Optional[str]]:
    """Build a clone URL from repo manifest remote fetch and project name."""
    fetch = fetch.strip()
    name = name.strip().lstrip("/")
    if not fetch:
        return "", "Remote fetch is empty"

    if fetch.startswith(("./", "../")) or fetch in {".", ".."}:
        return "", f"Relative remote fetch cannot be resolved without repo server context: {fetch}"

    repo_name = name if name.endswith(".git") else f"{name}.git"
    base = fetch.rstrip("/")
    separator = "" if base.endswith(":") else "/"
    return f"{base}{separator}{repo_name}", None
