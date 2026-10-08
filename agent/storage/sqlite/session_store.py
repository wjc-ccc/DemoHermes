"""Session / Turn 事实与 Memory review outbox 的 SQLite Store。"""
from __future__ import annotations
import json
import logging
import uuid
from ...core.memory.memory_scope import MemoryScope
from ...core.memory.types.memory_source_types import SessionReset, TurnRecord
from ...core.types import Message, Session, ToolCall
from .codec import milliseconds
from .errors import AuditConflictError, SessionIntegrityError
from .execution_store import insert_turn, insert_session_facts
from .job_store import enqueue_job
from .types import ArchivedMessage

logger = logging.getLogger(__name__)

def _escape_like(text):
    return text.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')

class SqliteSessionArchiveReader:
    def __init__(self, database):
        self.db = database

    def search_message_candidates(self, scope, wanted, limit):
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT m.session_id,m.message_id,m.content_text,m.created_at FROM messages m "
                "JOIN sessions s ON m.session_id=s.session_id WHERE s.scope_key=? "
                "AND py_casefold(m.content_text) LIKE ? ESCAPE '\\' ORDER BY m.created_at DESC LIMIT ?",
                (scope.storage_key,f'%{_escape_like(wanted.casefold())}%',limit)).fetchall()
        return [ArchivedMessage(*r[:3],r[3]/1000) for r in rows]

class SqliteSessionStore:
    """一个深 Store interface：会话恢复、Turn 原子提交和 outbox 调度。"""

    def __init__(self, database, *, executions, jobs, id_factory=None, extractor_version='memory-extractor-v3', review_scheduler=None, card_mutator=None):
        self.db = database
        self._db_path = database.path
        self.executions = executions
        self.jobs = jobs
        self._id_factory = id_factory
        self._extractor_version = extractor_version
        self._review_scheduler = review_scheduler
        self._card_mutator = card_mutator

    def _new_session(self, session_key, channel, author_id):
        return Session(session_id=self._id_factory() if self._id_factory else str(uuid.uuid4()),
                       session_key=session_key,channel=channel,author_id=author_id)

    def find(self, session_key):
        with self.db.read() as conn:
            conn.execute('BEGIN')
            row = conn.execute('SELECT session_id FROM sessions WHERE session_key=? AND is_current=1',
                               (session_key,)).fetchone()
            return self._load_session(conn,row[0]) if row else None

    def get_session(self, session_id):
        with self.db.read() as conn:
            conn.execute('BEGIN')
            return self._load_session(conn,session_id)

    def get_or_create(self, session_key, *, scope, channel='', author_id=''):
        with self.db.write() as conn:
            row = conn.execute('SELECT session_id,scope_key FROM sessions WHERE session_key=? AND is_current=1',
                               (session_key,)).fetchone()
            if row:
                if row[1] != scope.storage_key:
                    raise SessionIntegrityError(f'session_key={session_key} 的 scope 与请求 scope 不一致')
                return self._load_session(conn,row[0])
            session = self._new_session(session_key,channel,author_id)
            self._upsert_session(conn,session,scope)
            return session

    def commit_turn(self, session, turn, *, scope):
        """同一事务提交 Session、Turn、事实和待审 outbox。"""
        if turn.session_id != session.session_id:
            raise SessionIntegrityError('Turn 属于其他会话')
        with self.db.write() as conn:
            self._upsert_session(conn,session,scope)
            sequence = insert_turn(conn,turn)
            insert_session_facts(conn,session,turn)
            if self._card_mutator is not None and turn.completed and not turn.interrupted:
                for call in session.tool_calls:
                    if (call.trajectory_id != turn.turn_id or call.tool_name != 'memory_edit'
                        or call.status != 'success' or not isinstance(call.tool_call_result, dict)):
                        continue
                    mutation = call.tool_call_result.get('memory_mutation')
                    if mutation is not None:
                        item = self._card_mutator.apply(conn, scope, turn.user_message_id, mutation)
            if self._review_scheduler is not None:
                self._review_scheduler.record_turn(conn, turn)
            else:
                enqueue_job(conn,job_type='memory.extract',handler_version=self._extractor_version,
                    session_id=session.session_id,turn_id=turn.turn_id,job_id=turn.turn_id,
                    dedupe_key=f'memory.extract:{self._extractor_version}:{turn.turn_id}',
                    input={'turn_id':turn.turn_id,'from_turn_no':sequence,'to_turn_no':sequence,
                           'trigger_type':'turn_committed'})

    def close_and_reset(self, session_key, session, *, scope, channel='', author_id=''):
        with self.db.write() as conn:
            active = conn.execute('SELECT session_id FROM sessions WHERE session_key=? AND is_current=1',
                                  (session_key,)).fetchone()
            if not active or active[0] != session.session_id:
                raise SessionIntegrityError('被重开的会话已经不是当前会话')
            closed = session.model_copy(update={'status':'completed'})
            self._upsert_session(conn,closed,scope)
            insert_session_facts(conn,closed,None)
            conn.execute('UPDATE sessions SET is_current=0 WHERE session_id=?',(closed.session_id,))
            new_session = self._new_session(session_key,channel,author_id)
            self._upsert_session(conn,new_session,scope)
            if self._review_scheduler is not None:
                self._review_scheduler.flush_scope(conn, scope.storage_key, trigger_type='reset')
            return SessionReset(closed_session=closed,new_session=new_session)

    def archive(self, session_id):
        with self.db.write() as conn:
            session = self._load_session(conn,session_id)
            if session is None:
                return False
            now = milliseconds()
            payload = session.model_copy(update={'status':'completed','updated_at':now/1000}).model_dump_json(exclude={'messages','tool_calls'})
            conn.execute("UPDATE sessions SET is_current=0,status='completed',updated_at=?,payload_json=? "
                         "WHERE session_id=?",(now,payload,session_id))
            return True

    def diagnostics(self):
        # 此方法检查写连接配置，不执行迁移或写入数据。
        from .database import connect
        conn = connect(self._db_path)
        try:
            return dict(journal_mode=conn.execute('PRAGMA journal_mode').fetchone()[0],
                        foreign_keys=bool(conn.execute('PRAGMA foreign_keys').fetchone()[0]),
                        busy_timeout=conn.execute('PRAGMA busy_timeout').fetchone()[0],
                        schema_versions=tuple(r[0] for r in conn.execute('SELECT version FROM schema_migrations ORDER BY version')))
        finally:
            conn.close()

    def list_sessions(self):
        with self.db.read() as conn:
            rows = conn.execute('SELECT s.session_key,s.session_id,s.channel,s.author_id,s.last_active_at,'
                '(SELECT COUNT(*) FROM messages m WHERE m.session_id=s.session_id) FROM sessions s '
                'WHERE s.is_current=1 ORDER BY s.last_active_at DESC').fetchall()
        return [dict(session_key=r[0],session_id=r[1],channel=r[2],author_id=r[3],
                     last_active_at=r[4]/1000,message_count=r[5]) for r in rows]

    def list_debug_sessions(self, *, scope_key=None, limit=100):
        """Return active and archived sessions for the read-only debug UI."""
        where = 'WHERE s.scope_key=?' if scope_key else ''
        args = (scope_key,) if scope_key else ()
        with self.db.read() as conn:
            rows = conn.execute('SELECT s.session_id,s.session_key,s.status,s.channel,s.author_id,'
                's.created_at,s.updated_at,s.last_active_at,'
                '(SELECT COUNT(*) FROM turns t WHERE t.session_id=s.session_id),'
                '(SELECT COUNT(*) FROM messages m WHERE m.session_id=s.session_id) FROM sessions s '+where+
                ' ORDER BY s.last_active_at DESC,s.session_id DESC LIMIT ?',(*args,min(500,max(1,limit)))).fetchall()
        return [dict(session_id=r[0],session_key=r[1],status=r[2],channel=r[3],author_id=r[4],
                     created_at=r[5]/1000,updated_at=r[6]/1000,last_active_at=r[7]/1000,
                     turn_count=r[8],message_count=r[9]) for r in rows]

    @staticmethod
    def _upsert_session(conn, session, scope):
        previous = conn.execute('SELECT scope_key,session_key FROM sessions WHERE session_id=?',
                                (session.session_id,)).fetchone()
        if previous and previous != (scope.storage_key,session.session_key):
            raise SessionIntegrityError('已有 Session 的 scope 或 route 不可更改')
        payload = json.dumps(session.model_dump(mode='json',exclude={'messages','tool_calls'}),ensure_ascii=False)
        conn.execute('INSERT INTO sessions (session_id,session_key,scope_kind,scope_key,organization_id,'
            'tenant_id,user_id,project_id,status,channel,author_id,created_at,updated_at,last_active_at,payload_json) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET '
            'status=excluded.status,updated_at=excluded.updated_at,last_active_at=excluded.last_active_at,'
            'payload_json=excluded.payload_json',
            (session.session_id,session.session_key,scope.scope_kind,scope.storage_key,scope.organization_id,
             scope.tenant_id,scope.user_id,scope.project_id,session.status,session.channel,session.author_id,
             milliseconds(session.created_at),milliseconds(session.updated_at),milliseconds(session.last_active_at),payload))

    @staticmethod
    def _load_session(conn, session_id):
        row = conn.execute('SELECT payload_json FROM sessions WHERE session_id=?',(session_id,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        messages = [Message.model_validate_json(item[0]) for item in conn.execute(
            'SELECT payload_json FROM messages WHERE session_id=? ORDER BY seq_id',(session_id,))]
        tool_calls = [ToolCall.model_validate_json(item[0]) for item in conn.execute(
            'SELECT payload_json FROM tool_calls WHERE session_id=? ORDER BY created_at,tool_call_id',(session_id,))]
        return Session(**payload,messages=messages,tool_calls=tool_calls)
