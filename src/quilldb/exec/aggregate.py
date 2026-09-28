"""Aggregate functions and the operator that drives them (chapter 18
§18.5-§18.6, week7-query-processing.md §42).


AGGREGATES maps a BoundAggregate.func name to the three functions that turn
it into a running fold over a Volcano-model child stream: init() seeds one
group's state before any row has been seen, step() folds one more row's
argument value into that state, and final() converts the finished state
into the one Value the aggregate contributes to its output row. Keeping
these three apart -- rather than one function tracking a running result --
is what makes AVG correct: (sum, count) as the running state, dividing only
in final(), never computes a running AVERAGE OF AVERAGES, which silently
drifts on unequal batch sizes (§18.5).


MIN/MAX compare through compare_keys() (btree/index.py) rather than
Python's `<`/`>` -- the same cross-type order (NULL < numeric < text <
blob) an index descent already needs, so `MIN(mixed_type_column)` doesn't
raise TypeError the way `min(1, 'a')` would.
"""


from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from quilldb.btree.index import compare_keys
from quilldb.codec.record import Value
from quilldb.errors import IntegerOverflowError

# Reused rather than redefined: SUM's int64 boundary is the same one
# exec/expressions.py's _fit_int64 already checks arithmetic results
# against, and a second copy of 2**63-1 here would be one more place for
# the two to silently drift apart.
from quilldb.exec.expressions import _MAX_INT64, _MIN_INT64, Row, _require_number, evaluate
from quilldb.exec.operators import Operator, _explain_line
from quilldb.sql.binder import BoundAggregate, BoundExpression

type AggState = Any
type Number = int | float


@dataclass(frozen=True)
class AggSpec:
    init: Callable[[], AggState]
    step: Callable[[AggState, Value], AggState]
    final: Callable[[AggState], Value]


def _count_step(state: int, value: Value) -> int:
    return state if value is None else state + 1


def _sum_step(state: Value, value: Value) -> Value:
    """Fold one more row's value into a running SUM.

    `state` is the running sum so far -- None before the first non-NULL
    value has been seen. NULLs are skipped, same as every aggregate in this
    module (see `_min_step`/`_max_step` below for the identical shape).

    Deliberately NOT built on exec/expressions.py's `+`
    (week7-query-processing.md §42's named trap): that operator's
    `_fit_int64` silently promotes an int64 overflow to REAL -- correct for
    `+`, verified against sqlite3 -- but SQLite's SUM() raises "integer
    overflow" instead, and ONLY while every input so far has been an
    INTEGER. The moment a REAL input has been seen, SUM behaves like
    ordinary float addition forever after and never raises again, even if
    the running total is astronomically large. Both halves verified
    against sqlite3, not invented:

        SUM of all-INTEGER values overflowing 2**63-1  -> raises
        SUM where at least one value has been REAL      -> ordinary float
                                                            addition, never
                                                            raises
    """
    if value is None:
        return state  # skip NULLs, running total unchanged
    value = _require_number(value, "SUM")
    if state is None:
        return value  # first non-NULL value seen: it becomes the running total

    assert isinstance(state, (int, float)), (
        "state is always numeric here -- the None case returned above, and "
        "every other value ever stored into state already passed _require_number"
    )
    result: Number
    if isinstance(state, int) and isinstance(value, int):
        result = state + value
        if result < _MIN_INT64 or result > _MAX_INT64:
            raise IntegerOverflowError("SUM of all-INTEGER values overflowing 2**63-1")
    else:
        result = state + value

    return result


def _avg_step(state: tuple[Number | None, int], value: Value) -> tuple[Number | None, int]:
    total, count = state
    if value is None:
        return state
    number = _require_number(value, "AVG")
    return (number if total is None else total + number, count + 1)


def _avg_final(state: tuple[Number | None, int]) -> Value:
    total, count = state
    if total is None:
        return None  # no non-NULL value was ever seen -- count is 0 too
    return total / count


def _min_step(state: Value, value: Value) -> Value:
    if value is None:
        return state
    if state is None:
        return value
    return value if compare_keys([value], [state]) < 0 else state


def _max_step(state: Value, value: Value) -> Value:
    if value is None:
        return state
    if state is None:
        return value
    return value if compare_keys([value], [state]) > 0 else state


AGGREGATES: dict[str, AggSpec] = {
    "count_star": AggSpec(lambda: 0, lambda s, _: s + 1, lambda s: s),
    "count": AggSpec(lambda: 0, _count_step, lambda s: s),
    "sum": AggSpec(lambda: None, _sum_step, lambda s: s),
    "avg": AggSpec(lambda: (None, 0), _avg_step, _avg_final),
    "min": AggSpec(lambda: None, _min_step, lambda s: s),
    "max": AggSpec(lambda: None, _max_step, lambda s: s),
}


class HashAggregate(Operator):
    """dict from group key -> per-aggregate state (chapter 18 §18.6).

    quilldb uses a hash here where SQLite sorts, and that's a DEFENSIBLE
    difference rather than a divergence: SQLite avoids adding a hash table
    to a C library targeting embedded devices, and Python gives you a dict
    for free.

    `group_by` is empty for a plain aggregate query with no GROUP BY at
    all -- see the "no GROUP BY" paragraph below. Otherwise the group key
    is `tuple(evaluate(expr, row) for expr in group_by)`; every Value the
    codec produces (int, float, str, bytes, bool, None) is hashable, so
    that tuple is a legal dict key with no canonicalizing needed. NULL is
    a valid key like any other: `GROUP BY nullable_col` puts every NULL
    row in one group together, because `None == None` and `hash(None)` is
    well-defined, so nothing here has to special-case it.

    Each output row is the FLAT layout BoundAggregateSelect's docstring
    describes: `(*group key values, *finished aggregate values)`, in that
    order -- select_items/having (bound by sql/binder.py's
    _bind_group_output) are BoundExpression trees whose BoundColumn.index
    values point straight into this row, which is what lets build_operator
    finish the query with an ordinary Filter/Project on top instead of a
    special-cased evaluator.

    PIPELINE BREAKER, the same shape Sort will be: open() drains the child
    completely before next() can return anything, because SUM/COUNT/etc.
    only have an answer once every row in a group has been folded in.


    No GROUP BY at all (`group_by == ()`) = exactly one group, ALWAYS --
    next() returns a row even when the child produced zero rows
    (`SELECT COUNT(*) FROM empty_table` is one row containing 0, not zero
    rows). Seeding that one group before the child is pulled even once is
    what makes it fall out naturally: the loop below simply never runs its
    body for an empty child, and final() still turns the untouched init()
    state into a real value (0 for COUNT, NULL for SUM/AVG/MIN/MAX). A
    REAL GROUP BY gets no such seeding -- zero input rows means zero
    groups means zero output rows, same as any other empty result.
    """

    def __init__(
        self, child: Operator, group_by: tuple[BoundExpression, ...], aggregates: tuple[BoundAggregate, ...]
    ) -> None:
        self.child = child
        self.group_by = group_by
        self.aggregates = aggregates
        self._results: list[Row] = []
        self._position = 0

    def open(self, outer: Row = ()) -> None:
        self.child.open(outer)
        self._results = []
        self._position = 0
        try:
            groups: dict[tuple[Value, ...], list[AggState]] = {}
            if not self.group_by:
                groups[()] = [AGGREGATES[agg.func].init() for agg in self.aggregates]

            row = self.child.next()
            while row is not None:
                key = tuple(evaluate(expr, row) for expr in self.group_by)
                states = groups.get(key)
                if states is None:
                    states = [AGGREGATES[agg.func].init() for agg in self.aggregates]
                    groups[key] = states
                for i, agg in enumerate(self.aggregates):
                    value = None if agg.arg is None else evaluate(agg.arg, row)
                    states[i] = AGGREGATES[agg.func].step(states[i], value)
                row = self.child.next()

            self._results = [
                (
                    *key,
                    *(
                        AGGREGATES[agg.func].final(state)
                        for agg, state in zip(self.aggregates, states, strict=True)
                    ),
                )
                for key, states in groups.items()
            ]
        finally:
            # Drained fully above -- unlike SeqScan/IndexScan, there is no
            # standing cursor position for a later next() to resume from,
            # so the child's resources are released here, not in close().
            self.child.close()

    def next(self) -> Row | None:
        if self._position >= len(self._results):
            return None
        row = self._results[self._position]
        self._position += 1
        return row

    def close(self) -> None:
        self._results = []
        self._position = 0
        self.child.close()  # idempotent -- already closed by open() on the success path

    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        label = "HashAggregate" if not self.group_by else f"HashAggregate GROUP BY {len(self.group_by)} key(s)"
        return _explain_line(depth, label) + "\n" + self.child.explain(depth + 1, verbose)
