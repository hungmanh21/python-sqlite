"""Durable schema metadata stored in SQLite's real sqlite_schema table.

Not a new file format: sqlite_schema is an ordinary table b-tree page,
rooted at SCHEMA_ROOT_PAGE (page 1) -- the exact same slotted-page and
record encoding used for every user table. Every row's payload is
encode_record((type, name, tbl_name, rootpage, sql)), SQLite's own five
fixed columns (docs/implementation/week-3-sql.md §16), because `sqlite3`
itself has to be able to read this table for `sqlite3 file.db ".schema"`
and `PRAGMA integrity_check` to mean anything as an acceptance test.

Storing the raw CREATE TABLE text (not structured column metadata) and
re-parsing it in load() is deliberate, not a shortcut: it's the same
"survive version skew" argument as `user_version` (chapter 01 §1.6) -- a
newer quilldb can reinterpret an older file's schema without a migration.
load() is where that re-parse happens, once per open.

SCOPE LIMIT, deliberate and documented: this catalog works page 1 directly
rather than through BTree, so the schema never splits -- page 1 holds
PAGE_SIZE - 100 bytes, which measures out to 53 tables at the length of
`CREATE TABLE t (id INTEGER, name TEXT, age INTEGER)`, and create_table()
raises PageFullError beyond that. Page 1 is the one page whose b-tree header
sits at byte 100 instead of 0 (PageBody.header_offset), and teaching
BTree/TableCursor to thread that offset through every descent and split is a
bigger change than a fifty-table ceiling justifies here. If that ceiling ever
matters, the fix is to thread page_header_offset() through btree.py's
parse_page/write_page_body calls, not to change this file.

TableSchema objects are cached in `_tables` after load(); nothing here
keeps a BTree or cursor open between calls -- every method reads what it
needs and releases it before returning, so a Catalog is safe to hold for
the lifetime of a Connection.
"""

from quilldb.btree.cells import decode_leaf_table_cell, encode_leaf_table_cell
from quilldb.catalog.schema import ColumnSchema, TableSchema
from quilldb.codec.record import decode_record, encode_record
from quilldb.constants import SCHEMA_ROOT_PAGE, PageType
from quilldb.errors import (
    CorruptDatabaseError,
    PageFullError,
    TableAlreadyExistsError,
    TableNotFoundError,
    UnsupportedFeatureError,
)
from quilldb.sql.ast import CreateTable
from quilldb.sql.parser import parse
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.page import PageBody, parse_page, serialize_page, write_page_body
from quilldb.storage.pager import Pager, page_header_offset

_RESERVED_PREFIX = "sqlite_"
_CATALOG_COLUMN_COUNT = 5


class Catalog:
    def __init__(self, pager: Pager, pool: BufferPool) -> None:
        self.pager = pager
        self.pool = pool
        self._tables: dict[str, TableSchema] = {}

    def load(self) -> None:
        """Scan page 1, validate its five-column rows, and rebuild `_tables`.

        For every row with type == "table", parse the stored SQL and
        require a CreateTable whose name agrees with this row's `name`
        and `tbl_name`.

        Raises:
            CorruptDatabaseError: a malformed catalog record, a duplicate
                name, an impossible root page, or stored SQL inconsistent
                with the row it came from.
            UnsupportedFeatureError: a schema object this catalog cannot
                interpret safely (anything with type != "table" -- indexes,
                views, and triggers don't exist yet).
        """
        tables: dict[str, TableSchema] = {}

        for row in self._catalog_rows():
            if len(row) != _CATALOG_COLUMN_COUNT:
                raise CorruptDatabaseError(
                    f"sqlite_schema row has {len(row)} columns, expected {_CATALOG_COLUMN_COUNT}"
                )
            object_type, name, tbl_name, root_page, sql = row

            if object_type != "table":
                raise UnsupportedFeatureError(f"sqlite_schema holds a {object_type!r} object; only tables are supported")

            if not isinstance(name, str) or not isinstance(tbl_name, str) or not isinstance(sql, str):
                raise CorruptDatabaseError(f"sqlite_schema row for {name!r} has a non-text name/tbl_name/sql")
            if not isinstance(root_page, int):
                raise CorruptDatabaseError(f"sqlite_schema row for {name!r} has a non-integer rootpage")
            if not 1 <= root_page <= self.pager.page_count:
                raise CorruptDatabaseError(
                    f"sqlite_schema row for {name!r} claims rootpage {root_page}, "
                    f"outside 1..{self.pager.page_count}"
                )
            if tbl_name != name:
                raise CorruptDatabaseError(f"sqlite_schema row {name!r} has mismatched tbl_name {tbl_name!r}")

            statement = self._reparse(name, sql)
            if statement.name.casefold() != name.casefold():
                raise CorruptDatabaseError(
                    f"sqlite_schema row {name!r} stores SQL that creates {statement.name!r} instead"
                )

            folded = name.casefold()
            if folded in tables:
                raise CorruptDatabaseError(f"sqlite_schema holds two tables named {name!r}")
            tables[folded] = TableSchema(
                name=name,
                columns=tuple(ColumnSchema(c.name, c.data_type) for c in statement.columns),
                root_page=root_page,
                sql=sql,
            )

        self._tables = tables

    def create_table(self, statement: CreateTable, sql: str) -> TableSchema:
        """Allocate an empty table root and add one sqlite_schema row.

        Stores `sql` verbatim -- it is what a later load() re-parses, and
        what `sqlite3 file.db ".schema"` shows a user.

        Args:
            statement: the parsed CREATE TABLE (name + column defs).
            sql: the exact source text to store, verbatim.
        Returns:
            The new table's schema.
        Raises:
            TableAlreadyExistsError: a case-insensitive name collision with
                an existing table.
            UnsupportedFeatureError: the name starts with the reserved
                `sqlite_` prefix.
            PageFullError: page 1 has no room for another schema row (see
                this module's documented scope limit). The root page
                allocated for the doomed table is returned to the freelist
                first, so the file still passes `PRAGMA integrity_check`.
        """
        # Both rejections happen before anything is allocated or written, so a
        # refused CREATE TABLE leaves the file byte-identical.
        folded = statement.name.casefold()
        if folded.startswith(_RESERVED_PREFIX):
            raise UnsupportedFeatureError(f"table name {statement.name!r} uses the reserved {_RESERVED_PREFIX!r} prefix")
        if folded in self._tables:
            raise TableAlreadyExistsError(f"table {statement.name!r} already exists")

        root_page = self.pager.allocate_page()

        _, schema_body = self._schema_page()
        rowid = self._next_catalog_rowid(schema_body)
        payload = encode_record(("table", statement.name, statement.name, root_page, sql))
        cell_len = len(encode_leaf_table_cell(rowid, len(payload), payload))

        # Page 1's room is checked HERE -- after allocate_page(), which wrote
        # the new page through the pager directly, but before this page is ever
        # touched through the BufferPool. That ordering is what makes the
        # free_page() below safe: nothing is cached for `root_page` yet, so
        # reclaiming it can't be undone by a later pool flush. Initialize the
        # root first and the pool's dirty copy would overwrite the freelist
        # trunk free_page() just wrote, turning a leaked page into a corrupt
        # freelist (a LEAF_TABLE type byte read as a trunk pointer).
        if not schema_body.fits(cell_len):
            self.pager.free_page(root_page)
            raise PageFullError(
                f"sqlite_schema (page 1) has no room for table {statement.name!r}: the catalog "
                "lives on page 1 alone and never splits (see this module's scope limit)"
            )

        with self.pool.pinned(root_page, dirty=True) as raw:
            raw[:] = serialize_page(PageBody(PageType.LEAF_TABLE))

        # Publish the catalog row only after the root exists on disk: the
        # reverse order would leave a catalog row pointing at a page that was
        # never initialized.
        self._append_catalog_row(rowid, payload)
        self.pager.bump_schema_cookie()

        schema = TableSchema(
            name=statement.name,
            columns=tuple(ColumnSchema(c.name, c.data_type) for c in statement.columns),
            root_page=root_page,
            sql=sql,
        )
        self._tables[folded] = schema
        return schema

    def get_table(self, name: str) -> TableSchema:
        """Return a table's schema by case-insensitive name.

        Raises:
            TableNotFoundError: no table with this name exists.
        """
        try:
            return self._tables[name.casefold()]
        except KeyError:
            raise TableNotFoundError(f"no such table: {name}") from None

    def list_tables(self) -> tuple[TableSchema, ...]:
        """Return every table's schema, sorted by casefolded name."""
        return tuple(schema for _, schema in sorted(self._tables.items()))

    # ---- page 1 access ------------------------------------------------
    # Three small helpers so the four public methods above never touch a
    # cell pointer or a header offset directly.

    def _schema_page(self) -> tuple[bytearray, PageBody]:
        """Read page 1 and decode it, honoring its 100-byte header offset.
        The page is left UNPINNED -- callers that write must re-pin.
        """
        raw = self.pool.get_page(SCHEMA_ROOT_PAGE)
        try:
            body = parse_page(raw, page_header_offset(SCHEMA_ROOT_PAGE))
        finally:
            self.pool.unpin(SCHEMA_ROOT_PAGE)
        if body.page_type is not PageType.LEAF_TABLE:
            raise CorruptDatabaseError(f"page 1 is {body.page_type!r}, not a LEAF_TABLE sqlite_schema root")
        return raw, body

    def _catalog_rows(self) -> list[tuple[object, ...]]:
        """Every sqlite_schema row, decoded, in rowid order.

        Raises:
            CorruptDatabaseError: a row's payload spilled to an overflow
                chain. A CREATE TABLE statement long enough to overflow is
                beyond this catalog's scope, and silently reading only the
                local prefix would truncate the stored SQL.
            MalformedRecordError: propagated from decode_record.
        """
        _, body = self._schema_page()
        rows: list[tuple[object, ...]] = []
        for cell in body.cells:
            _, total_payload_len, local_payload, overflow_page = decode_leaf_table_cell(bytes(cell))
            if overflow_page or len(local_payload) != total_payload_len:
                raise CorruptDatabaseError("a sqlite_schema row spilled to overflow; unsupported")
            rows.append(decode_record(local_payload))
        return rows

    def _next_catalog_rowid(self, body: PageBody) -> int:
        """One past the largest rowid on `body` (page 1), starting at 1.

        Takes the already-decoded page rather than re-reading page 1, so a
        caller that also needs the body for a free-space check reads it once.

        Catalog rowids are the schema table's own keys -- entirely
        independent of any user table's rowids, which live in that table's
        own b-tree.
        """
        if not body.cells:
            return 1
        rowids = [decode_leaf_table_cell(bytes(cell))[0] for cell in body.cells]
        return max(rowids) + 1

    def _append_catalog_row(self, rowid: int, payload: bytes) -> None:
        """Add one cell to page 1, keeping cells in ascending rowid order.

        Raises:
            PageFullError: page 1 is out of room (see the module's scope limit).
        """
        cell = encode_leaf_table_cell(rowid, len(payload), payload)
        with self.pool.pinned(SCHEMA_ROOT_PAGE, dirty=True) as raw:
            body = parse_page(raw, page_header_offset(SCHEMA_ROOT_PAGE))
            index = len(body.cells)
            for i, existing in enumerate(body.cells):
                if decode_leaf_table_cell(bytes(existing))[0] > rowid:
                    index = i
                    break
            body.insert_cell(index, cell)
            write_page_body(raw, body)

    def _reparse(self, name: str, sql: str) -> CreateTable:
        """Re-parse a stored CREATE TABLE statement.

        Raises:
            CorruptDatabaseError: the stored text does not parse, or parses
                as something other than CREATE TABLE.
        """
        try:
            statement = parse(sql)
        except Exception as exc:
            raise CorruptDatabaseError(f"sqlite_schema row {name!r} stores unparseable SQL: {sql!r}") from exc
        if not isinstance(statement, CreateTable):
            raise CorruptDatabaseError(f"sqlite_schema row {name!r} stores a non-CREATE TABLE statement")
        return statement
