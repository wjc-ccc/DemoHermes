"""The only application entry point that initializes SQLite."""
from .database import Database, connect
from .migrations import migrate
from .observe_queries import ObserveQueries
from .session_store import SqliteSessionStore, SqliteSessionArchiveReader
from .execution_store import SqliteExecutionStore
from .job_store import SqliteJobStore
from .memory_store import SqliteMemoryStore
from .document_registry import SqliteDocumentRegistry
from .trace_store import SqliteTraceStore
from .audit_store import SqliteAuditStore
from .workspace_store import SqliteWorkspaceStore
from .narrative_store import SqliteNarrativeStore
from .state_store import SqliteStateStore
from .memory_review_committer import SqliteMemoryReviewCommitter


class RuntimeStorage:
    @classmethod
    def open(cls, db_path, *, id_factory=None, extractor_version="memory-review-v1"):
        database=Database(db_path)
        conn=connect(database.path)
        try:
            migrate(conn)
        finally:
            conn.close()
        return cls(database,id_factory=id_factory,extractor_version=extractor_version)

    def __init__(self, database, *, id_factory=None, extractor_version="memory-review-v1"):
        self.db=database
        self.observe=ObserveQueries(database)
        self.executions=SqliteExecutionStore(database)
        self.jobs=SqliteJobStore(database)
        self.sessions=SqliteSessionStore(database,executions=self.executions,jobs=self.jobs,
                                       id_factory=id_factory,extractor_version=extractor_version)
        self.archive=SqliteSessionArchiveReader(database)
        self.memory=SqliteMemoryStore(database)
        self.documents=SqliteDocumentRegistry(database)
        self.traces=SqliteTraceStore(database)
        self.audit=SqliteAuditStore(database)
        self.workspaces=SqliteWorkspaceStore(database)
        self.narrative=SqliteNarrativeStore(database)
        self.state=SqliteStateStore(database)
        self.review_committer=SqliteMemoryReviewCommitter(database)
