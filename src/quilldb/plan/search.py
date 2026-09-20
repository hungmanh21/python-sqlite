"""Stage 4 of the cost-based pipeline (chapter 12 §12.6): picking the
winning AccessPath out of everything stage 1-3 produced.


Chapter 12 §12.6 stage 4 also covers up-to-three-table joins and ORDER BY
interaction (comparing an ordered index path against an unordered path
plus a Sort). Neither joins nor ORDER BY exist anywhere else in quilldb
yet -- there's no join operator, no Sort operator, no `ORDER BY` in the
AST -- so choose_access_path() below is scoped to exactly what the rest
of the codebase can act on today: picking the minimum-cost path for ONE
table. Extending it to joins/ordering is future work for whichever
session actually builds those operators, not something to guess the
shape of now.
"""


from quilldb.plan.planner import AccessPath


def choose_access_path(candidates: list[AccessPath]) -> AccessPath:
    """Pick the minimum-cost.total candidate.


    Call this AFTER every candidate has been through estimate_row_counts()
    and assign_cost() -- it reads `path.cost.total`, which stage 3 fills
    in and stages 1-2 leave at `None`.


    Args:
        candidates: every legal AccessPath for one table, as produced by
            enumerate_access_paths() and then costed by assign_cost().
            Never empty in practice -- enumerate_access_paths() always
            includes a seq_scan (chapter 12 §12.6 trap #2) -- but don't
            assume that here; an empty list is a caller bug, not a case
            to silently paper over.


    Returns:
        The candidate with the lowest `cost.total`. Ties are legal (a
        seq_scan and a full-key index seek can cost the same on a tiny
        table) and any tiebreak is fine -- the two candidates are
        interchangeable by definition once their costs are equal.


    This is deliberately the simplest possible reduction over `cost.total`
    -- chapter 12 §12.6 is explicit that the *hard* part of stage 4 was
    already done by stage 3 assigning honest costs. A boolean-style index
    matching half the table should lose to a seq_scan here not because of
    a special case in this function, but because assign_cost() already
    priced it worse (chapter 12 §12.6 trap #2). If this function needs an
    if/elif per AccessPath.kind to get the right answer, that's a sign a
    cost is wrong upstream, not that this function needs to get smarter.
    """
    return min(candidates, key=_cost_total)




def _cost_total(path: AccessPath) -> float:
    assert path.cost is not None, "choose_access_path requires every candidate to be costed by assign_cost() first"
    return path.cost.total