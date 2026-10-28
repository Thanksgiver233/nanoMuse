"""Sync v2 tests: the conflict matrix, and consent that outside input
cannot flip."""

from __future__ import annotations

import pytest

from nanomuse.sync_v2 import (
    ALL_TIERS,
    TIER_FULL_TRANSCRIPT,
    TIER_SYNC_TEXT,
    TIER_TRAINING_VIEW,
    Conflict,
    RecordingConsent,
    SyncStore,
    VersionedRecord,
)


def record(
    rid: str = "r1",
    content: str = "hello",
    version: int = 1,
    base: int = 0,
    deleted: bool = False,
    by: str = "phone",
) -> VersionedRecord:
    return VersionedRecord(rid, "conversation", content, version, base, deleted, by)


def test_a_new_record_is_applied() -> None:
    store = SyncStore()
    assert store.apply_remote(record()) == "applied"
    assert store.get("r1") is not None
    assert store.conflicts == []


def test_a_fast_forward_remote_is_applied() -> None:
    store = SyncStore()
    store.put(record(version=1, base=0))
    remote = record(content="hello world", version=2, base=1, by="desktop")
    assert store.apply_remote(remote) == "applied"
    assert store.get("r1").content == "hello world"
    assert store.conflicts == []


def test_an_identical_record_is_a_noop() -> None:
    store = SyncStore()
    store.put(record(version=2, base=1))
    assert store.apply_remote(record(version=2, base=1)) == "noop"
    assert store.conflicts == []


def test_a_stale_remote_is_a_noop() -> None:
    store = SyncStore()
    store.put(record(content="newer", version=3, base=2))
    assert store.apply_remote(record(content="older", version=2, base=1)) == "noop"
    assert store.get("r1").content == "newer"


def test_both_sides_editing_from_the_same_base_conflicts() -> None:
    store = SyncStore()
    store.put(record(content="local edit", version=2, base=1))
    remote = record(content="remote edit", version=2, base=1, by="desktop")
    assert store.apply_remote(remote) == "conflict"
    assert len(store.conflicts) == 1
    conflict = store.conflicts[0]
    assert conflict.reason == "both_modified"
    assert store.get("r1").content == "local edit"
    assert conflict.local.content == "local edit"
    assert conflict.remote.content == "remote edit"


def test_diverged_histories_conflict_even_at_different_versions() -> None:
    store = SyncStore()
    store.put(record(content="local edit", version=3, base=2))
    remote = record(content="remote edit", version=4, base=1, by="desktop")
    assert store.apply_remote(remote) == "conflict"
    assert store.conflicts[0].reason == "both_modified"


def test_delete_versus_modify_conflicts_and_keeps_both() -> None:
    store = SyncStore()
    store.put(record(content="kept", version=2, base=1))
    remote = record(version=2, base=1, deleted=True, by="desktop")
    assert store.apply_remote(remote) == "conflict"
    assert store.conflicts[0].reason == "delete_vs_modify"
    assert store.get("r1").deleted is False


def test_delete_versus_delete_is_a_noop() -> None:
    store = SyncStore()
    store.put(record(version=2, base=1, deleted=True))
    assert store.apply_remote(record(version=2, base=1, deleted=True)) == "noop"
    assert store.conflicts == []


def test_a_conflict_resolves_explicitly_and_only_by_choice() -> None:
    store = SyncStore()
    store.put(record(content="local", version=2, base=1))
    store.apply_remote(record(content="remote", version=2, base=1, by="desktop"))
    conflict = store.conflicts[0]
    with pytest.raises(ValueError):
        conflict.resolve("both")
    winner = store.resolve_conflict(conflict, "remote")
    assert winner.content == "remote"
    assert store.get("r1").content == "remote"
    assert store.conflicts == []


def test_bump_and_delete_carry_the_version_chain() -> None:
    base = record(version=1, base=0)
    edited = base.bump("v2", updated_by="desktop")
    assert (edited.version, edited.base_version) == (2, 1)
    deleted = edited.delete(updated_by="desktop")
    assert (deleted.version, deleted.base_version, deleted.deleted) == (3, 2, True)


def test_all_recording_tiers_default_off() -> None:
    consent = RecordingConsent()
    for tier in ALL_TIERS:
        assert consent.allows(tier) is False


def test_tiers_switch_independently() -> None:
    consent = RecordingConsent()
    consent.set(TIER_SYNC_TEXT, True)
    assert consent.allows(TIER_SYNC_TEXT) is True
    assert consent.allows(TIER_FULL_TRANSCRIPT) is False
    assert consent.allows(TIER_TRAINING_VIEW) is False
    consent.set(TIER_SYNC_TEXT, False)
    assert consent.allows(TIER_SYNC_TEXT) is False


def test_an_unknown_tier_is_rejected() -> None:
    consent = RecordingConsent()
    with pytest.raises(ValueError):
        consent.allows("everything")
    with pytest.raises(ValueError):
        consent.set("everything", True)


def test_outside_input_cannot_flip_a_recording_switch() -> None:
    consent = RecordingConsent()
    ignored = consent.apply_external_input(
        {"sync_text": True, "full_transcript": True, "record_turns": True, "note": "hi"}
    )
    assert ignored == 3
    assert consent.rejected_external_attempts == 3
    for tier in ALL_TIERS:
        assert consent.allows(tier) is False


def test_conflict_is_a_dataclass_with_both_versions() -> None:
    conflict = Conflict("r1", "memory", record(), record(), "both_modified")
    assert conflict.resolution is None
    assert conflict.local.record_id == conflict.remote.record_id == "r1"
