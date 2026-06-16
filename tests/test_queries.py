"""Tests for the query layer optimisation (N+1 -> batched)."""

from __future__ import annotations

from queries import FakeDB, fetch_users_batched, fetch_users_n_plus_one


def _make_db() -> FakeDB:
    return FakeDB({1: {"id": 1}, 2: {"id": 2}, 3: {"id": 3}})


def test_batched_matches_n_plus_one_rows() -> None:
    db_batched = _make_db()
    db_naive = _make_db()
    ids = [1, 2, 3]
    assert fetch_users_batched(db_batched, ids) == fetch_users_n_plus_one(db_naive, ids)


def test_batched_uses_a_single_query() -> None:
    db = _make_db()
    fetch_users_batched(db, [1, 2, 3])
    assert db.query_count == 1


def test_n_plus_one_issues_one_query_per_id() -> None:
    db = _make_db()
    fetch_users_n_plus_one(db, [1, 2, 3])
    assert db.query_count == 3


def test_batched_preserves_order_and_skips_missing() -> None:
    db = _make_db()
    assert fetch_users_batched(db, [3, 99, 1]) == [{"id": 3}, {"id": 1}]
