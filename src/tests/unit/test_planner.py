"""enumerate_access_paths tests: legality only (stage 1 of chapter 12's
pipeline), no cost estimates yet -- these check WHICH candidates get
generated, not which one wins.


The composite-index cases are the worked table from chapter 12 §12.3,
verified there against real sqlite3 -- reusing it here means passing these
tests is evidence _match_index_prefix implements the actual leading-column
rule, not just a rule that happens to agree on cases we made up ourselves.
"""


from quilldb.catalog.schema import ColumnSchema, IndexSchema, TableSchema
from quilldb.plan.planner import AccessPath, enumerate_access_paths
from quilldb.plan.predicates import Predicate
from quilldb.sql.ast import DataType
from quilldb.sql.binder import BoundLiteral


_TABLE = TableSchema(
    "t",
    (
        ColumnSchema("a", DataType.INTEGER),
        ColumnSchema("b", DataType.INTEGER),
        ColumnSchema("c", DataType.INTEGER),
        ColumnSchema("d", DataType.INTEGER),
        ColumnSchema("e", DataType.INTEGER),
    ),
    root_page=2,
    sql="CREATE TABLE t (a INTEGER, b INTEGER, c INTEGER, d INTEGER, e INTEGER)",
)


_ABC = IndexSchema("abc", "t", ("a", "b", "c"), unique=False, root_page=3, sql="CREATE INDEX abc ON t(a,b,c)")




def _eq(column: str, value: object = 1) -> Predicate:
    literal = BoundLiteral(value)
    return Predicate(column, "=", literal, literal)




def _cmp(column: str, operator: str, value: object = 1) -> Predicate:
    literal = BoundLiteral(value)
    return Predicate(column, operator, literal, literal)




def _index_paths(paths: list[AccessPath]) -> list[AccessPath]:
    return [p for p in paths if p.kind == "index_scan"]




# =====================================================================
# SeqScan is unconditional (chapter 12 §12.6 trap #2)
# =====================================================================




def test_seq_scan_always_present_even_with_no_predicates():
    paths = enumerate_access_paths(_TABLE, [], [])
    assert len(paths) == 1
    assert paths[0].kind == "seq_scan"
    assert paths[0].residual == ()




def test_seq_scan_present_alongside_a_usable_index():
    paths = enumerate_access_paths(_TABLE, [_ABC], [_eq("a")])
    seq_scans = [p for p in paths if p.kind == "seq_scan"]
    assert len(seq_scans) == 1




# =====================================================================
# The leading-column rule, one query at a time -- chapter 12 §12.3's table
# =====================================================================




def test_a_equals_seeks_prefix_of_one():
    predicates = [_eq("a")]
    (path,) = _index_paths(enumerate_access_paths(_TABLE, [_ABC], predicates))
    assert [p.column for p in path.seek_terms] == ["a"]
    assert path.residual == ()




def test_a_and_b_equals_seeks_prefix_of_two():
    predicates = [_eq("a"), _eq("b")]
    (path,) = _index_paths(enumerate_access_paths(_TABLE, [_ABC], predicates))
    assert [p.column for p in path.seek_terms] == ["a", "b"]
    assert path.residual == ()




def test_a_b_c_equals_seeks_the_whole_index():
    predicates = [_eq("a"), _eq("b"), _eq("c")]
    (path,) = _index_paths(enumerate_access_paths(_TABLE, [_ABC], predicates))
    assert [p.column for p in path.seek_terms] == ["a", "b", "c"]
    assert path.residual == ()




def test_a_equals_b_range_sandwiches_two_inequalities():
    predicates = [_eq("a"), _cmp("b", ">", 2), _cmp("b", "<", 5)]
    (path,) = _index_paths(enumerate_access_paths(_TABLE, [_ABC], predicates))
    assert [p.column for p in path.seek_terms] == ["a", "b", "b"]
    assert path.residual == ()




def test_c_dropped_when_b_is_only_constrained_by_inequality():
    """`a=1 AND b>2 AND c=3` -> seeks (a=? AND b>?); `c` is to the right of
    an inequality and must land in residual, not be silently dropped
    (chapter 12 §12.6 trap #1).
    """
    a, b, c = _eq("a"), _cmp("b", ">", 2), _eq("c", 3)
    (path,) = _index_paths(enumerate_access_paths(_TABLE, [_ABC], [a, b, c]))
    assert [p.column for p in path.seek_terms] == ["a", "b"]
    assert path.residual == (c,)




def test_c_dropped_when_b_has_no_predicate_at_all():
    """`a=1 AND c=3` -> seeks (a=?) only; the gap at `b` means `c` cannot
    be reached even though it has a usable equality predicate.
    """
    a, c = _eq("a"), _eq("c", 3)
    (path,) = _index_paths(enumerate_access_paths(_TABLE, [_ABC], [a, c]))
    assert [p.column for p in path.seek_terms] == ["a"]
    assert path.residual == (c,)




def test_c_alone_offers_no_seek_at_all():
    """`WHERE c=3` with no constraint on `a`: the index's first column has
    no predicate, so this index must not appear as a candidate.
    """
    paths = enumerate_access_paths(_TABLE, [_ABC], [_eq("c", 3)])
    assert _index_paths(paths) == []




def test_in_counts_as_equality_for_prefix_purposes():
    """Modeled here as two "=" predicates on the same column, standing in
    for `a IN (1,2)` -- both should be consumable and `b` should still be
    reachable, matching optoverview.html's "IN counts as equality".
    """
    predicates = [_eq("a", 1), _eq("a", 2), _eq("b", 2)]
    (path,) = _index_paths(enumerate_access_paths(_TABLE, [_ABC], predicates))
    columns = [p.column for p in path.seek_terms]
    assert columns.count("a") == 2
    assert columns.count("b") == 1
    assert path.residual == ()