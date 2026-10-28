"""Sync v2: versioned records, explicit conflicts, tiered recording consent.

This module lives outside the ``nanomuse.sync`` package on purpose:
that package's ``__init__`` pulls in the server, and the server pulls
the sync package back, so importing anything from inside it first in
a fresh interpreter fails on the cycle.  Sync v2 has no server
dependency and stays importable on its own.

Every synced record (a conversation or a memory entry) carries a
version.  A remote record is applied only when the versions tell a
story with one ending: a newer version wins, an equal version with
equal content is a no-op.  When both sides changed the same record
from the same base version, or one side deleted what the other side
changed, the result is an explicit :class:`Conflict` object handed to
the caller.  Nothing is ever silently dropped: a conflict must be
resolved by a person (or by a stated rule the person chose), and until
then both versions are kept.

Recording consent is three separate switches, one per tier, and all
three default to OFF.  The switches are read, never rewritten, by
anything that handles outside input: an inbound record, message, or
frame that claims to change consent is ignored, and the attempt is
reported.  The existing record defaults in the codebase are untouched;
this module only adds the reading logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class VersionedRecord:
    """One synced record: conversation text or one memory entry."""

    record_id: str
    kind: str  # "conversation" | "memory"
    content: str
    version: int = 1
    base_version: int = 0
    deleted: bool = False
    updated_by: str = ""

    def bump(self, content: str, *, updated_by: str) -> VersionedRecord:
        return VersionedRecord(
            record_id=self.record_id,
            kind=self.kind,
            content=content,
            version=self.version + 1,
            base_version=self.version,
            updated_by=updated_by,
        )

    def delete(self, *, updated_by: str) -> VersionedRecord:
        return VersionedRecord(
            record_id=self.record_id,
            kind=self.kind,
            content=self.content,
            version=self.version + 1,
            base_version=self.version,
            deleted=True,
            updated_by=updated_by,
        )


@dataclass
class Conflict:
    """An explicit, unresolved disagreement between two versions.

    Both sides are kept.  ``resolution`` is None until a person (or
    their chosen rule) picks one; applying a resolution is a separate,
    auditable step, never something sync does on its own.
    """

    record_id: str
    kind: str
    local: VersionedRecord
    remote: VersionedRecord
    reason: str
    resolution: str | None = None

    def resolve(self, choice: str) -> VersionedRecord:
        if choice not in ("local", "remote"):
            raise ValueError(f"resolution must be 'local' or 'remote', not {choice!r}")
        self.resolution = choice
        return self.local if choice == "local" else self.remote


class SyncStore:
    """A local store of versioned records with explicit conflict handling."""

    def __init__(self) -> None:
        self._records: dict[str, VersionedRecord] = {}
        self.conflicts: list[Conflict] = []

    def put(self, record: VersionedRecord) -> None:
        self._records[record.record_id] = record

    def get(self, record_id: str) -> VersionedRecord | None:
        return self._records.get(record_id)

    def apply_remote(self, remote: VersionedRecord) -> str:
        """Apply one remote record.  Returns "applied", "noop", or
        "conflict".  A conflict is recorded and both versions are kept;
        the local record is left untouched until resolved."""
        local = self._records.get(remote.record_id)
        if local is None:
            self._records[remote.record_id] = remote
            return "applied"
        if remote.content == local.content and remote.deleted == local.deleted:
            return "noop"
        if remote.version <= local.version:
            if remote.version == local.version:
                reason = "delete_vs_modify" if remote.deleted != local.deleted else "both_modified"
                self.conflicts.append(
                    Conflict(
                        record_id=remote.record_id,
                        kind=remote.kind,
                        local=local,
                        remote=remote,
                        reason=reason,
                    )
                )
                return "conflict"
            return "noop"
        if remote.base_version == local.version:
            self._records[remote.record_id] = remote
            return "applied"
        reason = "delete_vs_modify" if remote.deleted != local.deleted else "both_modified"
        self.conflicts.append(
            Conflict(
                record_id=remote.record_id,
                kind=remote.kind,
                local=local,
                remote=remote,
                reason=reason,
            )
        )
        return "conflict"

    def resolve_conflict(self, conflict: Conflict, choice: str) -> VersionedRecord:
        winner = conflict.resolve(choice)
        self._records[winner.record_id] = winner
        self.conflicts.remove(conflict)
        return winner


TIER_SYNC_TEXT = "sync_text"
TIER_FULL_TRANSCRIPT = "full_transcript"
TIER_TRAINING_VIEW = "training_view"
ALL_TIERS = (TIER_SYNC_TEXT, TIER_FULL_TRANSCRIPT, TIER_TRAINING_VIEW)


@dataclass
class RecordingConsent:
    """Three recording switches, each defaulting to OFF.

    ``grants`` is the settings mapping the caller owns; absent keys
    read as False.  Consent changes only through :meth:`set`, an
    explicit local call.  :meth:`apply_external_input` exists so the
    boundary is testable: outside input can never flip a switch.
    """

    grants: dict[str, bool] = field(default_factory=dict)
    rejected_external_attempts: int = 0

    def allows(self, tier: str) -> bool:
        if tier not in ALL_TIERS:
            raise ValueError(f"unknown recording tier: {tier!r}")
        return bool(self.grants.get(tier, False))

    def set(self, tier: str, allowed: bool) -> None:
        if tier not in ALL_TIERS:
            raise ValueError(f"unknown recording tier: {tier!r}")
        self.grants[tier] = allowed

    def apply_external_input(self, claimed: dict[str, Any]) -> int:
        """Ignore any consent change claimed by outside input.

        Returns the number of ignored claims.  External input must
        never change rules, grants, switches, or audit logs; consent
        is a switch, so it is covered by the same red line.
        """
        ignored = 0
        for key in claimed:
            if key in ALL_TIERS or key.startswith("record"):
                ignored += 1
        self.rejected_external_attempts += ignored
        return ignored
