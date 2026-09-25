"""工作台聚合的原子写入。这里只维护分工和显式状态，不驱动执行。"""
from datetime import datetime, timezone
import uuid
from ...core.memory.session_workspace import SessionWorkspace, WorkspaceItem, WorkspaceItemStatus, WorkspaceStatus
from ...core.memory.session_narrative import NarrativeItem
from .codec import milliseconds, dumps, utcnow
from .errors import RevisionConflictError, IntegrityError
from .references import require_session, require_fact, require_state_ref
from .narrative_store import append_event, append_contract

def _check_plan(conn, session_id, items):
    by_id = {i.item_id:i for i in items}
    if len(by_id) != len(items):
        raise IntegrityError('DAG 节点 ID 重复')
    for item in items:
        if item.session_id != session_id or not set(item.dependencies) <= by_id.keys():
            raise IntegrityError('依赖不存在或节点跨会话')
        for ref in item.input_refs:
            require_state_ref(conn,session_id,ref)
        if item.turn_id:
            require_fact(conn,'turns','turn_id',session_id,item.turn_id)
        for identity in item.tool_call_ids:
            require_fact(conn,'tool_calls','tool_call_id',session_id,identity)
    visiting, visited = set(), set()
    def visit(identity):
        if identity in visiting:
            raise IntegrityError('DAG 不允许环')
        if identity in visited:
            return
        visiting.add(identity)
        for dependency in by_id[identity].dependencies:
            visit(dependency)
        visiting.remove(identity)
        visited.add(identity)
    for identity in by_id:
        visit(identity)

def _put_item(conn, item):
    conn.execute('INSERT INTO workspace_items VALUES (?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(session_id,item_id) DO UPDATE SET status=excluded.status,revision=excluded.revision,'
        'executor_ref=excluded.executor_ref,dependencies_json=excluded.dependencies_json,'
        'payload_json=excluded.payload_json,updated_at=excluded.updated_at',
        (item.item_id,item.session_id,item.status.value,item.revision,item.executor_ref,
         dumps(item.dependencies),item.model_dump_json(),milliseconds(item.created_at),milliseconds(item.updated_at)))

class SqliteWorkspaceStore:
    def __init__(self, database):
        self.db = database

    @staticmethod
    def _item(conn, session_id, item_id):
        row = conn.execute('SELECT payload_json FROM workspace_items WHERE session_id=? AND item_id=?',
                           (session_id,item_id)).fetchone()
        return WorkspaceItem.model_validate_json(row[0]) if row else None

    @staticmethod
    def _get(conn, session_id):
        row = conn.execute('SELECT session_id,goal_id,status,revision,plan_revision,contract_revision,'
                           'current_item_id,pause_reason,created_at,updated_at FROM session_workspaces '
                           'WHERE session_id=?',(session_id,)).fetchone()
        if not row:
            return None
        items = tuple(WorkspaceItem.model_validate_json(r[0]) for r in conn.execute(
            'SELECT payload_json FROM workspace_items WHERE session_id=? ORDER BY item_id',(session_id,)))
        return SessionWorkspace(session_id=row[0],goal_id=row[1],status=row[2],revision=row[3],
            plan_revision=row[4],contract_revision=row[5],current_item_id=row[6],pause_reason=row[7],
            created_at=datetime.fromtimestamp(row[8]/1000,timezone.utc),
            updated_at=datetime.fromtimestamp(row[9]/1000,timezone.utc),items=items)

    def get(self, session_id):
        with self.db.read() as conn:
            conn.execute('BEGIN')
            return self._get(conn,session_id)

    def get_item(self, session_id, item_id):
        with self.db.read() as conn:
            return self._item(conn,session_id,item_id)

    def create(self, workspace: SessionWorkspace, *, contract=None):
        workspace = SessionWorkspace.model_validate(workspace.model_dump())
        if workspace.revision != 1 or workspace.plan_revision != 1 or workspace.contract_revision != 0:
            raise RevisionConflictError('新工作台版本必须从 1 开始，契约尚未提交')
        if any(i.revision != 1 for i in workspace.items):
            raise RevisionConflictError('新节点版本必须从 1 开始')
        with self.db.write() as conn:
            require_session(conn,workspace.session_id)
            if self._get(conn,workspace.session_id):
                raise RevisionConflictError('会话已有工作台')
            _check_plan(conn,workspace.session_id,workspace.items)
            if workspace.current_item_id and workspace.current_item_id not in {i.item_id for i in workspace.items}:
                raise IntegrityError('当前节点不存在')
            conn.execute('INSERT INTO session_workspaces VALUES (?,?,?,?,?,?,?,?,?,?)',
                (workspace.session_id,workspace.goal_id,workspace.status.value,1,1,0,
                 workspace.current_item_id,workspace.pause_reason,
                 milliseconds(workspace.created_at),milliseconds(workspace.updated_at)))
            for item in workspace.items:
                _put_item(conn,item)
            if contract is not None:
                if contract.session_id != workspace.session_id:
                    raise IntegrityError('契约跨会话')
                append_contract(conn,contract,expected_revision=0)
            return self._get(conn,workspace.session_id)

    def replace_plan(self, session_id, items, *, expected_revision, actor, reason):
        if not actor or not reason:
            raise ValueError('计划变更需提供操作者和原因')
        items = tuple(WorkspaceItem.model_validate(i.model_dump()) for i in items)
        with self.db.write() as conn:
            workspace = self._get(conn,session_id)
            if workspace is None or workspace.revision != expected_revision:
                raise RevisionConflictError('workspace revision 冲突')
            previous = {i.item_id:i for i in workspace.items}
            incoming = {i.item_id:i for i in items}
            if len(incoming) != len(items):
                raise IntegrityError('节点 ID 重复')
            next_items = []
            for item in items:
                old = previous.get(item.item_id)
                if old:
                    if item.revision != old.revision:
                        raise RevisionConflictError('节点基于过期版本')
                    item = item.model_copy(update={'revision':old.revision+1,'created_at':old.created_at,'updated_at':utcnow()})
                elif item.revision != 1:
                    raise RevisionConflictError('新节点版本必须是 1')
                next_items.append(item)
            # 删除代表取消，旧节点及所有历史引用继续存在。
            next_items.extend(i.model_copy(update={'status':WorkspaceItemStatus.CANCELLED,
                'revision':i.revision+1,'updated_at':utcnow()}) for i in workspace.items if i.item_id not in incoming)
            _check_plan(conn,session_id,next_items)
            for item in next_items:
                _put_item(conn,item)
            current = workspace.current_item_id if workspace.current_item_id in incoming else None
            conn.execute('UPDATE session_workspaces SET revision=revision+1,plan_revision=plan_revision+1,'
                         'current_item_id=?,updated_at=? WHERE session_id=?',(current,milliseconds(),session_id))
            append_event(conn,NarrativeItem(item_id=str(uuid.uuid4()),session_id=session_id,
                event_type='plan_revised',what_happened='更新任务分工与依赖',reason=reason,author=actor,
                metadata={'plan_revision':workspace.plan_revision+1,
                          'items':[i.model_dump(mode='json') for i in next_items]}))
            return self._get(conn,session_id)

    def set_item_status(self, session_id, item_id, status, *, expected_revision, actor, reason,
                        turn_id=None, tool_call_ids=None):
        status = WorkspaceItemStatus(status)
        with self.db.write() as conn:
            old = self._item(conn,session_id,item_id)
            if old is None or old.revision != expected_revision:
                raise RevisionConflictError('item revision 冲突')
            changes = {'status':status,'revision':old.revision+1,'updated_at':utcnow(),
                       'blocked_reason':reason if status is WorkspaceItemStatus.BLOCKED else None}
            if turn_id is not None:
                require_fact(conn,'turns','turn_id',session_id,turn_id)
                changes['turn_id'] = turn_id
            if tool_call_ids is not None:
                for identity in tool_call_ids:
                    require_fact(conn,'tool_calls','tool_call_id',session_id,identity)
                changes['tool_call_ids'] = tuple(tool_call_ids)
            item = old.model_copy(update=changes)
            _put_item(conn,item)
            conn.execute('UPDATE session_workspaces SET revision=revision+1,updated_at=? WHERE session_id=?',
                         (milliseconds(),session_id))
            append_event(conn,NarrativeItem(item_id=str(uuid.uuid4()),session_id=session_id,
                event_type='item_status_changed',workspace_item_id=item_id,
                what_happened=f'{old.status.value} → {status.value}',reason=reason,author=actor))
            return item

    def set_status(self, session_id, status, *, expected_revision, actor, reason, current_item_id=None):
        status = WorkspaceStatus(status)
        with self.db.write() as conn:
            workspace = self._get(conn,session_id)
            if workspace is None or workspace.revision != expected_revision:
                raise RevisionConflictError('workspace revision 冲突')
            if current_item_id and current_item_id not in {i.item_id for i in workspace.items}:
                raise IntegrityError('当前节点不属于工作台')
            conn.execute('UPDATE session_workspaces SET status=?,current_item_id=?,pause_reason=?,'
                         'revision=revision+1,updated_at=? WHERE session_id=?',
                         (status.value,current_item_id,reason if status is WorkspaceStatus.PAUSED else None,
                          milliseconds(),session_id))
            append_event(conn,NarrativeItem(item_id=str(uuid.uuid4()),session_id=session_id,
                event_type='workspace_status_changed',what_happened=status.value,reason=reason,author=actor))
            return self._get(conn,session_id)
