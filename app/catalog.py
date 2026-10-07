"""Everything known about a workspace before any question is asked."""
from dataclasses import dataclass, field

from .ingestion import Workspace
from .profiling import Relationship, TableProfile, profile_workspace
from .traps import Issue, detect_issues


@dataclass
class Catalog:
    workspace: Workspace
    profiles: dict[str, TableProfile]
    relationships: list[Relationship]
    issues: list[Issue]
    origin: dict = field(default_factory=dict)  # table -> (fix store label, name there); set by the app

    @property
    def tables(self):
        return self.workspace.tables

    @property
    def metrics(self) -> dict:
        return self.workspace.metrics

    def issues_for(self, tables) -> list[Issue]:
        return [i for i in self.issues if i.table in tables]


def build_catalog(ws: Workspace) -> Catalog:
    profiles, rels = profile_workspace(ws.tables)
    return Catalog(ws, profiles, rels, detect_issues(ws.tables, profiles, rels))
