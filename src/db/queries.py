"""Database query layer demonstrating N+1 vs. batched access."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

Row = dict[str, object]


class FakeDB:
    """In-memory store that counts the number of queries issued."""

    def __init__(self, rows: Mapping[int, Row]) -> None:
        self._rows: dict[int, Row] = dict(rows)
        self.query_count = 0

    def fetch_one(self, user_id: int) -> Row | None:
        self.query_count += 1
        return self._rows.get(user_id)

    def fetch_many(self, user_ids: Iterable[int]) -> dict[int, Row]:
        self.query_count += 1
        wanted = set(user_ids)
        return {uid: row for uid, row in self._rows.items() if uid in wanted}


def fetch_users_n_plus_one(db: FakeDB, user_ids: list[int]) -> list[Row]:
    """One query per id (the anti-pattern being optimised away)."""
    found: list[Row] = []
    for uid in user_ids:
        row = db.fetch_one(uid)
        if row is not None:
            found.append(row)
    return found


def fetch_users_batched(db: FakeDB, user_ids: list[int]) -> list[Row]:
    """A single batched query, preserving request order, skipping misses."""
    found = db.fetch_many(user_ids)
    return [found[uid] for uid in user_ids if uid in found]

# touched by fake_llm during mutate
