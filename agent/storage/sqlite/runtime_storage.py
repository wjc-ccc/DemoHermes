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
from .memory_review_committer import SqliteMemoryReviewCommitter
from .memory_review_scheduler import SqliteMemoryReviewScheduler
from .memory_card_mutation import SqliteMemoryCardMutationService
from .eval_store import SqliteEvalStore
from agent.types.memory.extraction import MEMORY_EXTRACTOR_VERSION


class RuntimeStorage:
    @classmethod
    def open(cls, db_path, *, id_factory=None, extractor_version=None, extra_schema_dirs=()):
        extractor_version = extractor_version or MEMORY_EXTRACTOR_VERSION
        database=Database(db_path)
        conn=connect(database.path)
        try:
            migrate(conn, extra_schema_dirs=extra_schema_dirs)
        finally:
            conn.close()
        return cls(database,id_factory=id_factory,extractor_version=extractor_version)

    def __init__(self, database, *, id_factory=None, extractor_version=None):
        extractor_version = extractor_version or MEMORY_EXTRACTOR_VERSION
        self.db=database
        self.observe=ObserveQueries(database)
        self.executions=SqliteExecutionStore(database)
        self.jobs=SqliteJobStore(database)
        self.review_scheduler=SqliteMemoryReviewScheduler(database,extractor_version=extractor_version)
        self.review_scheduler.bootstrap()
        self.card_mutations=SqliteMemoryCardMutationService(database)
        self.sessions=SqliteSessionStore(database,executions=self.executions,jobs=self.jobs,
                                       id_factory=id_factory,extractor_version=extractor_version,
                                       review_scheduler=self.review_scheduler,
                                       card_mutator=self.card_mutations)
        self.archive=SqliteSessionArchiveReader(database)
        self.memory=SqliteMemoryStore(database)
        self.documents=SqliteDocumentRegistry(database)
        self.traces=SqliteTraceStore(database)
        self.audit=SqliteAuditStore(database)
        self.review_committer=SqliteMemoryReviewCommitter(database)
        self.evals=SqliteEvalStore(database)
        self.memory_evals=self.evals  # Legacy CLI name; both use the single generic store.
