"""Read-only projections; construction never initializes or migrates a database."""
from .database import Database
from .catalog import list_catalog, read_table_page
from .workspace_store import SqliteWorkspaceStore
from .narrative_store import SqliteNarrativeStore
from .state_store import SqliteStateStore

class ObserveQueries:
    def __init__(self, db: Database):
        self.db = db

    def catalog(self):
        return list_catalog({"agent": self.db.path})

    def table_page(self, table, *, limit=50, offset=0):
        return read_table_page({"agent": self.db.path},"agent",table,limit=limit,offset=offset)

    def session_states(self, session_id, *, limit=50, offset=0):
        limit=max(1,min(int(limit),100))
        offset=max(0,int(offset))
        with self.db.read() as conn:
            conn.execute("BEGIN")
            if not conn.execute("SELECT 1 FROM sessions WHERE session_id=?",(session_id,)).fetchone():
                return None
            workspace=SqliteWorkspaceStore._get(conn,session_id)
            contract=SqliteNarrativeStore._contract(conn,session_id)
            events=SqliteNarrativeStore._events(conn,session_id,limit,offset)
            state=SqliteStateStore._current(conn,session_id)
            total=conn.execute("SELECT COUNT(*) FROM narrative_events WHERE session_id=?",(session_id,)).fetchone()[0]
            return dict(session_id=session_id,
                workspace=workspace.model_dump(mode="json") if workspace else None,
                narrative=dict(contract=contract.model_dump(mode="json") if contract else None,
                               events=[e.model_dump(mode="json") for e in events],
                               total=total,limit=limit,offset=offset),
                state=state.model_dump(mode="json"))
