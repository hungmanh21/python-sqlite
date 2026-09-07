"""Exception hierarchy for quilldb.


Rule: anything derived from CorruptDatabaseError means the FILE is bad.
Anything else means the CALLER did something wrong, or is internal control flow.
"""




class QuillDBError(Exception):
    """Base for every error quilldb raises."""
   




class DatabaseError(QuillDBError):
    """A problem with a database file or its contents."""




class CorruptDatabaseError(DatabaseError):
    """The file violates the format. Never raise this for caller mistakes."""




class InvalidHeaderError(CorruptDatabaseError):
    """Magic bytes wrong, page size illegal, or header self-inconsistent."""




class PageOutOfRangeError(CorruptDatabaseError):
    """Asked for a page number outside the file."""




class InvalidPageTypeError(CorruptDatabaseError):
    """A page's type byte is not a known PageType."""




class MalformedCellError(CorruptDatabaseError):
    """A cell offset or length points outside its page."""




class MalformedRecordError(CorruptDatabaseError):
    """A record header or body is truncated or has a reserved serial type."""




class OverflowCycleError(CorruptDatabaseError):
    """An overflow page chain loops back on itself."""




class BTreeInvariantError(CorruptDatabaseError):
    """The validator found a structural violation."""




class PageFullError(QuillDBError):
    """Not enough room on a page. Internal control flow — triggers a split."""




class UnsupportedFeatureError(QuillDBError):
    """A valid file using a feature quilldb does not implement."""



