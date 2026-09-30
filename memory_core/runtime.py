from __future__ import annotations

from pathlib import Path
from typing import Any

from .governance import GovernancePolicy, ValidatedIntake
from .observation import validate_store
from .packet import PacketRenderer
from .profile import MemoryProfile
from .retrieval import CueDrivenRetriever
from .store import MemoryStore


class MemoryRuntime:
    """Consumer-neutral façade over the Memory Core mechanisms.

    The host supplies the profile, database path, seeds, permission boundary,
    and final authority. This class only wires the reusable store, intake,
    retrieval, packet, maintenance, and doctor surfaces together.
    """

    def __init__(
        self,
        db_path: str | Path,
        profile: MemoryProfile,
        *,
        surface: str = "local",
        governance: GovernancePolicy | None = None,
        pinned_guard: tuple[str, ...] = (),
    ):
        self.store = MemoryStore(db_path, pinned_guard=pinned_guard)
        self.profile = profile
        self.surface = surface
        self.intake = ValidatedIntake(
            self.store,
            surface=surface,
            policy=governance,
        )
        self.retriever = CueDrivenRetriever(self.store, profile)
        self.renderer = PacketRenderer(profile)

    def submit(self, **proposal: Any) -> dict[str, Any]:
        self.store.require_writable()
        return self.intake.submit(**proposal)

    def retrieve(
        self,
        query: str,
        *,
        scope: str = "global",
        limit: int = 8,
        token_budget: int = 1400,
        include_history: bool | None = None,
        track_access: bool = True,
        min_accessibility: float | None = None,
        wake_on_direct_cue: bool = True,
        wake_relation_types: tuple[str, ...] = (),
        access_gain: float = 0.01,
    ):
        return self.retriever.retrieve(
            query,
            scope=scope,
            surface=self.surface,
            limit=limit,
            token_budget=token_budget,
            include_history=include_history,
            track_access=track_access,
            min_accessibility=min_accessibility,
            wake_on_direct_cue=wake_on_direct_cue,
            wake_relation_types=wake_relation_types,
            access_gain=access_gain,
        )

    def retrieve_readonly(
        self,
        query: str,
        *,
        scope: str = "global",
        limit: int = 8,
        token_budget: int = 1400,
        include_history: bool | None = None,
    ):
        return self.retrieve(
            query,
            scope=scope,
            limit=limit,
            token_budget=token_budget,
            include_history=include_history,
            track_access=False,
        )

    def render_context(
        self,
        query: str,
        *,
        scope: str = "global",
        limit: int = 8,
        token_budget: int = 1400,
        include_history: bool | None = None,
        compact: bool = True,
    ) -> str:
        hits = self.retrieve(
            query,
            scope=scope,
            limit=limit,
            token_budget=token_budget,
            include_history=include_history,
        )
        return self.renderer.render(
            query,
            hits,
            scope=scope,
            surface=self.surface,
            compact=compact,
            token_budget=token_budget,
        )

    def apply_maintenance(self, **maintenance: Any) -> dict[str, Any]:
        self.store.require_writable()
        maintenance.setdefault("surface", self.surface)
        return self.store.apply_maintenance(**maintenance)

    def doctor(self) -> dict[str, Any]:
        return validate_store(self.store)
