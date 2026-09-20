"""choose_access_path tests: picking the minimum-cost candidate (stage 4
of chapter 12's pipeline).


Chapter 12 §12.6 trap #2's own example anchors the interesting case: a
low-selectivity index (~half the table) must lose to a plain SeqScan, not
because choose_access_path() special-cases it, but because assign_cost()
already priced it worse. If that test only passed by adding an
index-vs-seq_scan branch here, it would be evidence of a rule-based
planner wearing a cost-based costume.
"""


from quilldb.plan.planner import AccessPath, PlanCost
from quilldb.plan.search import choose_access_path


def _path(kind: str, total: float, startup: float = 0.0) -> AccessPath:
    return AccessPath(kind, None, (), (), cost=PlanCost(startup=startup, total=total))




# =====================================================================
# the basic reduction: lowest cost.total wins
# =====================================================================




def test_picks_the_only_candidate():
    seq = _path("seq_scan", 100.0)
    assert choose_access_path([seq]) is seq




def test_picks_the_cheaper_of_two_candidates():
    cheap = _path("index_scan", 24.01)
    expensive = _path("seq_scan", 200.0)
    assert choose_access_path([cheap, expensive]) is cheap




def test_candidate_order_in_the_list_does_not_matter():
    cheap = _path("index_scan", 10.0)
    expensive = _path("seq_scan", 500.0)
    assert choose_access_path([expensive, cheap]) is cheap




def test_picks_the_cheapest_of_several_candidates():
    a = _path("seq_scan", 50.0)
    b = _path("index_scan", 12.0)
    c = _path("index_scan", 30.0)
    assert choose_access_path([a, b, c]) is b




# =====================================================================
# ties: either candidate is an acceptable answer
# =====================================================================




def test_tie_returns_one_of_the_tied_candidates():
    a = _path("seq_scan", 42.0)
    b = _path("index_scan", 42.0)
    assert choose_access_path([a, b]) in (a, b)




# =====================================================================
# chapter 12 §12.6 trap #2: a low-selectivity index must lose on cost,
# not be excluded by a rule
# =====================================================================




def test_low_selectivity_index_loses_to_seq_scan_on_cost_alone():
    seq = _path("seq_scan", 210.0)
    boolean_index = _path("index_scan", 20_012.0, startup=12.0)  # half the table, priced by assign_cost()
    assert choose_access_path([seq, boolean_index]) is seq




def test_selective_index_beats_seq_scan_on_cost_alone():
    seq = _path("seq_scan", 210.0)
    selective_index = _path("index_scan", 24.01, startup=12.0)
    assert choose_access_path([seq, selective_index]) is selective_index