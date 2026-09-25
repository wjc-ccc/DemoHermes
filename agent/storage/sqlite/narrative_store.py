"""目标契约与叙事账本的只追加存储；工作集由调用方指定引用。"""
import uuid
from ...core.memory.session_narrative import GoalContract, NarrativeItem, SessionNarrative, WorkingSet
from .codec import milliseconds
from .errors import RevisionConflictError, IntegrityError
from .references import require_session, require_item, require_fact, require_state_ref

def append_event(conn, item):
    item = NarrativeItem.model_validate(item.model_dump())
    require_session(conn,item.session_id)
    require_item(conn,item.session_id,item.workspace_item_id)
    for ref in item.state_delta_refs:
        require_state_ref(conn,item.session_id,ref)
    existing = conn.execute('SELECT payload_json FROM narrative_events WHERE item_id=?',(item.item_id,)).fetchone()
    if existing:
        previous = NarrativeItem.model_validate_json(existing[0])
        if previous.model_dump(exclude={'sequence_no'}) != item.model_dump(exclude={'sequence_no'}):
            raise IntegrityError('账本 ID 已存在且内容不同')
        return previous
    seq = conn.execute('SELECT COALESCE(MAX(sequence_no),0)+1 FROM narrative_events WHERE session_id=?',
                       (item.session_id,)).fetchone()[0]
    stored = item.model_copy(update={'sequence_no':seq})
    conn.execute('INSERT INTO narrative_events VALUES (?,?,?,?,?,?,?,?)',
                 (stored.item_id,stored.session_id,seq,stored.event_type,stored.workspace_item_id,
                  stored.author,milliseconds(stored.created_at),stored.model_dump_json()))
    return stored

def append_contract(conn, contract, *, expected_revision):
    contract = GoalContract.model_validate(contract.model_dump())
    row = conn.execute('SELECT goal_id,contract_revision FROM session_workspaces WHERE session_id=?',
                       (contract.session_id,)).fetchone()
    if not row or row[0] != contract.goal_id:
        raise IntegrityError('目标契约与工作台不匹配')
    if row[1] != expected_revision or contract.revision != expected_revision+1:
        raise RevisionConflictError('contract revision 冲突')
    require_fact(conn,'messages','message_id',contract.session_id,contract.source_message_id,user_only=True)
    conn.execute('INSERT INTO goal_contract_versions VALUES (?,?,?,?,?,?,?,?)',
                 (contract.session_id,contract.revision,contract.goal_id,contract.source_message_id,
                  contract.changed_by,contract.change_reason,milliseconds(contract.created_at),
                  contract.model_dump_json()))
    conn.execute('UPDATE session_workspaces SET contract_revision=?,revision=revision+1,updated_at=? '
                 'WHERE session_id=?',
                 (contract.revision,milliseconds(),contract.session_id))
    append_event(conn,NarrativeItem(item_id=str(uuid.uuid4()),session_id=contract.session_id,
        event_type='contract_revised',what_happened=contract.user_goal,reason=contract.change_reason,
        author=contract.changed_by,metadata={'contract_revision':contract.revision,
                                           'source_message_id':contract.source_message_id}))
    return contract

class SqliteNarrativeStore:
    def __init__(self, database):
        self.db = database

    def append(self, item: NarrativeItem):
        with self.db.write() as conn:
            return append_event(conn,item)

    def append_contract(self, contract: GoalContract, *, expected_revision: int):
        with self.db.write() as conn:
            return append_contract(conn,contract,expected_revision=expected_revision)

    @staticmethod
    def _contract(conn, session_id, revision=None):
        sql = 'SELECT payload_json FROM goal_contract_versions WHERE session_id=?'
        args = [session_id]
        if revision is not None:
            sql += ' AND revision=?'
            args.append(revision)
        row = conn.execute(sql+' ORDER BY revision DESC LIMIT 1',args).fetchone()
        return GoalContract.model_validate_json(row[0]) if row else None

    def get_contract(self, session_id, revision=None):
        with self.db.read() as conn:
            return self._contract(conn,session_id,revision)

    def list_contracts(self, session_id, *, limit=100, offset=0):
        with self.db.read() as conn:
            return tuple(GoalContract.model_validate_json(r[0]) for r in conn.execute(
                'SELECT payload_json FROM goal_contract_versions WHERE session_id=? '
                'ORDER BY revision DESC LIMIT ? OFFSET ?', (session_id,min(100,max(1,limit)),max(0,offset))))

    @staticmethod
    def _events(conn, session_id, limit=100, offset=0):
        return tuple(NarrativeItem.model_validate_json(r[0]) for r in conn.execute(
            'SELECT payload_json FROM narrative_events WHERE session_id=? '
            'ORDER BY sequence_no DESC LIMIT ? OFFSET ?', (session_id,min(100,max(1,limit)),max(0,offset))))

    def list_events(self, session_id, *, limit=100, offset=0):
        with self.db.read() as conn:
            return self._events(conn,session_id,limit,offset)

    def get(self, session_id, *, limit=100, offset=0):
        with self.db.read() as conn:
            conn.execute("BEGIN")
            return SessionNarrative(session_id=session_id,contract=self._contract(conn,session_id),
                                    events=self._events(conn,session_id,limit,offset))

    def build_working_set(self, session_id, *, contract_revision=None, item_id=None,
                          event_ids=(), state_refs=()):
        from .workspace_store import SqliteWorkspaceStore
        from .state_store import SqliteStateStore
        with self.db.read() as conn:
            # 同一只读快照，避免目标、计划和引用来自不同提交。
            conn.execute('BEGIN')
            require_session(conn,session_id)
            contract = self._contract(conn,session_id,contract_revision)
            if contract_revision is not None and contract is None:
                raise IntegrityError('契约版本不存在')
            current = None
            if item_id:
                require_item(conn,session_id,item_id)
                current = SqliteWorkspaceStore._item(conn,session_id,item_id)
            events = []
            for identity in event_ids:
                row = conn.execute('SELECT payload_json FROM narrative_events WHERE session_id=? AND item_id=?',
                                   (session_id,identity)).fetchone()
                if not row:
                    raise IntegrityError('事件不存在或跨会话')
                events.append(NarrativeItem.model_validate_json(row[0]))
            states = []
            for ref in state_refs:
                require_state_ref(conn,session_id,ref)
                states.append(SqliteStateStore._get(conn,ref))
            return WorkingSet(session_id=session_id,contract=contract,current_item=current,
                              events=tuple(events),state_items=tuple(states))
