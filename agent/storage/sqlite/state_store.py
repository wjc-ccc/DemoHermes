"""机械态的版本追加、精确读取与显式失效。"""
from decimal import Decimal, InvalidOperation
import uuid
from ...core.memory.session_state import SessionStateItem, SessionState, StateRef, StateValidity
from .codec import milliseconds, dumps, utcnow
from .errors import RevisionConflictError, IntegrityError
from .references import require_session, require_item, require_fact, require_state_ref

def _check_value(item):
    value = item.value
    checks = {'string': lambda: isinstance(value,str),
              'integer': lambda: type(value) is int,
              'boolean': lambda: type(value) is bool,
              'object': lambda: isinstance(value,dict),
              'array': lambda: isinstance(value,list),
              'null': lambda: value is None}
    if item.value_type == 'decimal':
        try:
            valid = isinstance(value,str) and Decimal(value).is_finite()
        except InvalidOperation:
            valid = False
    else:
        valid = checks[item.value_type]()
    if not valid:
        raise IntegrityError('value 与 value_type 不一致；decimal 必须是精确字符串')
    dumps(value)

def put_state(conn, item, *, expected_revision):
    item = SessionStateItem.model_validate(item.model_dump())
    _check_value(item)
    require_session(conn,item.session_id)
    require_item(conn,item.session_id,item.workspace_item_id)
    previous = conn.execute(
        'SELECT MAX(revision) FROM session_state_items WHERE session_id=? AND scope_key=? AND key=?',
        (item.session_id,item.scope_key,item.key)).fetchone()[0] or 0
    if previous != expected_revision or item.revision != expected_revision + 1:
        raise RevisionConflictError('state revision 冲突')
    for ref in item.provenance.parent_refs:
        require_state_ref(conn,item.session_id,ref)
    if item.provenance.source_type == 'user_input':
        require_fact(conn,'messages','message_id',item.session_id,item.provenance.source_id,user_only=True)
    elif item.provenance.source_type == 'tool_output':
        require_fact(conn,'tool_calls','tool_call_id',item.session_id,item.provenance.source_id)
    conn.execute('INSERT INTO session_state_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (item.item_id,item.session_id,item.scope_key,item.key,item.revision,item.validity.value,
         item.value_type,dumps(item.value),item.provenance.source_type,item.provenance.source_id,
         item.workspace_item_id,milliseconds(item.created_at),item.created_by,item.model_dump_json())
    )
    return item

class SqliteStateStore:
    def __init__(self, database):
        self.db = database

    def put(self, item: SessionStateItem, *, expected_revision: int) -> SessionStateItem:
        with self.db.write() as conn:
            return put_state(conn,item,expected_revision=expected_revision)

    @staticmethod
    def _get(conn, ref):
        row = conn.execute('SELECT payload_json FROM session_state_items WHERE session_id=? '
                           'AND scope_key=? AND key=? AND revision=?',
                           (ref.session_id,ref.scope_key,ref.key,ref.revision)).fetchone()
        return SessionStateItem.model_validate_json(row[0]) if row else None

    def get(self, ref: StateRef) -> SessionStateItem | None:
        with self.db.read() as conn:
            return self._get(conn,ref)

    def get_current(self, session_id, scope_key, key):
        with self.db.read() as conn:
            row = conn.execute('SELECT payload_json FROM session_state_items WHERE session_id=? '
                               'AND scope_key=? AND key=? ORDER BY revision DESC LIMIT 1',
                               (session_id,scope_key,key)).fetchone()
            return SessionStateItem.model_validate_json(row[0]) if row else None

    def list_history(self, session_id, scope_key, key, *, limit=100, offset=0):
        with self.db.read() as conn:
            rows = conn.execute('SELECT payload_json FROM session_state_items WHERE session_id=? '
                                'AND scope_key=? AND key=? ORDER BY revision DESC LIMIT ? OFFSET ?',
                                (session_id,scope_key,key,min(100,max(1,limit)),max(0,offset))).fetchall()
            return tuple(SessionStateItem.model_validate_json(r[0]) for r in rows)

    @staticmethod
    def _current(conn, session_id):
        rows = conn.execute('SELECT s.payload_json FROM session_state_items s WHERE session_id=? '
                            'AND revision=(SELECT MAX(t.revision) FROM session_state_items t '
                            'WHERE t.session_id=s.session_id AND t.scope_key=s.scope_key AND t.key=s.key) '
                            'ORDER BY s.scope_key,s.key', (session_id,)).fetchall()
        return SessionState(session_id=session_id,items=tuple(
            SessionStateItem.model_validate_json(r[0]) for r in rows))

    def list_current(self, session_id) -> SessionState:
        with self.db.read() as conn:
            return self._current(conn,session_id)

    def invalidate(self, ref: StateRef, *, reason: str, actor: str):
        if not reason.strip():
            raise ValueError('失效必须说明原因')
        with self.db.write() as conn:
            item = self._get(conn,ref)
            if item is None:
                raise IntegrityError('状态不存在')
            next_item = item.model_copy(update={'item_id':str(uuid.uuid4()),'revision':ref.revision+1,
                'validity':StateValidity.INVALIDATED,'invalidation_reason':reason,
                'created_at':utcnow(),'created_by':actor})
            return put_state(conn,next_item,expected_revision=ref.revision)
