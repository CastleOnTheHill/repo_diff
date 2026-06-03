"""Diff engine for comparing two manifest project lists."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from manifest_parser import Project, is_pinned_revision


@dataclass
class ChangedProject:
    """Represents a project that exists in both manifests with different revisions."""
    old: Project
    new: Project
    commits: List[Dict[str, str]] = field(default_factory=list)
    git_error: Optional[str] = None


@dataclass
class DiffResult:
    """Result of comparing two manifests."""
    added: List[Project] = field(default_factory=list)
    removed: List[Project] = field(default_factory=list)
    changed: List[ChangedProject] = field(default_factory=list)
    unchanged: List[Project] = field(default_factory=list)

    def summary(self) -> Dict[str, int]:
        return {
            "added": len(self.added),
            "removed": len(self.removed),
            "changed": len(self.changed),
            "unchanged": len(self.unchanged),
        }


class DiffEngine:
    """Compare two lists of projects and classify differences."""

    def diff(self, old_projects: List[Project], new_projects: List[Project]) -> DiffResult:
        old_by_name: Dict[str, Project] = {p.name: p for p in old_projects}
        new_by_name: Dict[str, Project] = {p.name: p for p in new_projects}

        result = DiffResult()

        # Added: in new but not in old
        for name, proj in new_by_name.items():
            if name not in old_by_name:
                result.added.append(proj)

        # Removed: in old but not in new
        for name, proj in old_by_name.items():
            if name not in new_by_name:
                result.removed.append(proj)

        # Common: check revision changes
        for name in old_by_name:
            if name in new_by_name:
                old_proj = old_by_name[name]
                new_proj = new_by_name[name]
                if old_proj.revision != new_proj.revision:
                    result.changed.append(ChangedProject(old=old_proj, new=new_proj))
                elif not is_pinned_revision(new_proj.revision):
                    result.changed.append(ChangedProject(old=old_proj, new=new_proj))
                else:
                    result.unchanged.append(new_proj)

        # Sort for stable output
        result.added.sort(key=lambda p: p.name)
        result.removed.sort(key=lambda p: p.name)
        result.changed.sort(key=lambda c: c.new.name)
        result.unchanged.sort(key=lambda p: p.name)

        return result
