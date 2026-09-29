"""A compiled-plan cache keyed by SQL text (week7-query-processing.md §43,
"The plan cache"): reuse a single-table query's winning access-path SHAPE
across repeated executions of the same SQL text, skipping stage 1-3's
per-candidate enumeration and costing -- the quill_stat1 reads stage 3
needs -- on every call after the first.

What gets cached is `PlanShape` (plan/planner.py), not the AccessPath a
build actually uses: an AccessPath's `seek_terms`/`residual` are Predicate
objects carrying THIS bind's own BoundExpression values baked in (for
`WHERE id = ?`, literally this call's own parameter). Caching and reusing
those directly for a later call with a different `?` would answer every
later call with the first call's value -- exactly the trap this session's
own plan-cache spec warns never to fall into. PlanShape has no bound value
in it at all; plan/planner.py's `rebuild_access_path` re-derives a fresh,
this-call's-own-values AccessPath from a cached PlanShape plus the fresh
predicates every bind() call already produces.

Scope, documented rather than silent: this cache only ever stores a
single-table PlanShape (exec/operators.py's `_build_single_table_source`,
shared by a plain SELECT and a no-GROUP-BY aggregate SELECT). Caching a
JOIN's chosen PlanCandidate -- order, one AccessPath per table, and every
match expression -- is a real further saving (join enumeration is the
factorial-cost part of planning), but PlanCandidate's match_expressions
carry the same kind of bind-specific BoundExpression values an AccessPath's
Predicates do, and reducing THAT to a value-free, safely-reusable shape is
more surface than this session covers. A joined SELECT always plans fresh.
"""

from dataclasses import dataclass

from quilldb.plan.planner import PlanShape


@dataclass(frozen=True)
class _Entry:
    schema_cookie: int
    shape: PlanShape


class PlanCache:
    """One per Database (api/database.py), shared by every Connection open
    on that file -- schema_cookie is already the cross-connection
    invalidation signal a DDL/ANALYZE from ANY connection bumps
    (storage/pager.py's bump_schema_cookie, catalog/catalog.py, and
    plan/analyze.py all call it), so a per-Connection cache would just
    mean every Connection separately re-learns the same shape instead of
    sharing one.

    Invalidation is lazy, by comparing `schema_cookie` in `get()` -- the
    same "notice on next use" policy Connection.execute() already applies
    to resyncing its own Catalog after another connection's committed DDL
    (week6-concurrency.md SS37.4) -- rather than proactively sweeping
    every entry the moment the cookie moves. A stale entry is simply never
    returned; `put()`'s fresh write with the CURRENT cookie is what
    replaces it.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def get(self, sql: str, schema_cookie: int) -> PlanShape | None:
        entry = self._entries.get(sql)
        if entry is None or entry.schema_cookie != schema_cookie:
            return None
        return entry.shape

    def put(self, sql: str, schema_cookie: int, shape: PlanShape) -> None:
        self._entries[sql] = _Entry(schema_cookie, shape)
