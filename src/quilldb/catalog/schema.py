"""In-memory schema objects reconstructed from sqlite_schema.


Metadata only -- no live BTree, Pager, or cursor here. A TableSchema must
stay safe to cache in Catalog._tables and hand to a binder long after the
scan that produced it has released its pins (catalog/catalog.py's
docstring goes into why load() has to work this way).
"""


from dataclasses import dataclass

from quilldb.errors import ColumnNotFoundError
from quilldb.sql.ast import DataType


@dataclass(frozen=True)
class ColumnSchema:
    name: str
    data_type: DataType




@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[ColumnSchema, ...]
    root_page: int
    sql: str


    def column_index(self, name: str) -> int:
        """Resolve an unquoted column name to its position, case-insensitively.


        Args:
            name: the column name as written in the query.
        Returns:
            The zero-based index into `columns`.
        Raises:
            ColumnNotFoundError: no column matches, casefolded.
        """
        folded = name.casefold()
        for index, column in enumerate(self.columns):
            if column.name.casefold() == folded:
                return index
        raise ColumnNotFoundError(f"no such column: {name!r} in table {self.name!r}")




@dataclass(frozen=True)
class IndexSchema:
    """An index's declared shape -- metadata only, same rule as TableSchema
    above: no live BTree, Pager, or cursor here.


    `columns` holds the DECLARED key columns by name, in index order --
    excluding the rowid that IndexBTree glues onto every stored key
    (chapter 11 §11.4). `table` is the indexed table's name, not the
    index's own -- matching sqlite_schema's `tbl_name` column, which is
    what lets Catalog.indexes_for() find every index on a table with one
    scan.
    """


    name: str
    table: str
    columns: tuple[str, ...]
    unique: bool
    root_page: int
    sql: str