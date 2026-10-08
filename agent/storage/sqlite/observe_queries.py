"""Read-only projections; construction never initializes or migrates a database."""
from .database import Database
from .catalog import list_catalog, read_table_page

class ObserveQueries:
    def __init__(self, db: Database):
        self.db = db

    def catalog(self):
        return list_catalog({"agent": self.db.path})

    def table_page(self, table, *, limit=50, offset=0):
        return read_table_page({"agent": self.db.path},"agent",table,limit=limit,offset=offset)
