"""parse_stat1/default_*_stats tests: the quill_stat1 K+1-integer decode
and the documented fallback for an unanalyzed table or index.
"""


import pytest


from quilldb.catalog.schema import IndexSchema
from quilldb.errors import CorruptDatabaseError
from quilldb.plan.statistics import (
    IndexStats,
    default_index_stats,
    default_table_stats,
    parse_stat1,
)


_ABC = IndexSchema("abc", "t", ("a", "b", "c"), unique=False, root_page=3, sql="CREATE INDEX abc ON t(a,b,c)")
_SINGLE = IndexSchema("ix_a", "t", ("a",), unique=False, root_page=4, sql="CREATE INDEX ix_a ON t(a)")




# =====================================================================
# parse_stat1: chapter 12 §12.5's worked example and its edge cases
# =====================================================================




def test_three_column_index_decodes_row_count_and_three_prefix_averages():
    stats = parse_stat1("10000 100 10 2", _ABC)
    assert stats == IndexStats(row_count=10_000, rows_per_prefix=(100, 10, 2))




def test_single_column_index_decodes_row_count_and_one_prefix_average():
    stats = parse_stat1("500 5", _SINGLE)
    assert stats == IndexStats(row_count=500, rows_per_prefix=(5,))




def test_unique_index_ends_in_one():
    """§12.5: 'if the index is unique, then the last integer will be 1.'"""
    stats = parse_stat1("500 1", _SINGLE)
    assert stats.rows_per_prefix[-1] == 1




def test_too_few_integers_is_corrupt():
    with pytest.raises(CorruptDatabaseError):
        parse_stat1("10000 100", _ABC)  # needs 4 integers for a 3-column index




def test_too_many_integers_is_corrupt():
    with pytest.raises(CorruptDatabaseError):
        parse_stat1("10000 100 10 2 1", _ABC)




def test_non_integer_is_corrupt():
    with pytest.raises(CorruptDatabaseError):
        parse_stat1("10000 many 10 2", _ABC)




# =====================================================================
# defaults: planning must never fail just because ANALYZE hasn't run
# =====================================================================




def test_default_table_stats_is_a_large_conservative_row_count():
    stats = default_table_stats()
    assert stats.row_count > 0




def test_default_index_stats_has_one_entry_per_index_column():
    stats = default_index_stats(_ABC, default_table_stats())
    assert len(stats.rows_per_prefix) == len(_ABC.columns) == 3




def test_default_index_stats_row_count_matches_table_row_count():
    table_stats = default_table_stats()
    stats = default_index_stats(_ABC, table_stats)
    assert stats.row_count == table_stats.row_count