"""三态存储使用的引用完整性检查；不包含业务校验或执行策略。"""
from .errors import IntegrityError

def require_session(conn, session_id):
    if not conn.execute('SELECT 1 FROM sessions WHERE session_id=?', (session_id,)).fetchone():
        raise IntegrityError('session 不存在')

def require_item(conn, session_id, item_id):
    if item_id is not None and not conn.execute(
        'SELECT 1 FROM workspace_items WHERE session_id=? AND item_id=?', (session_id,item_id)
    ).fetchone():
        raise IntegrityError('workspace item 不存在或属于其他会话')

def require_fact(conn, table, column, session_id, identity, *, user_only=False):
    # 表名和列名仅来自模块内部固定调用，不接受外部 SQL 标识符。
    row = conn.execute(f'SELECT session_id' + (',role' if user_only else '') +
                       f' FROM {table} WHERE {column}=?', (identity,)).fetchone()
    if not row or row[0] != session_id or (user_only and row[1] != 'user'):
        raise IntegrityError('来源不存在、不是用户消息或属于其他会话')

def require_state_ref(conn, session_id, ref):
    if ref.session_id != session_id or not conn.execute(
        'SELECT 1 FROM session_state_items WHERE session_id=? AND scope_key=? AND key=? AND revision=?',
        (session_id,ref.scope_key,ref.key,ref.revision)
    ).fetchone():
        raise IntegrityError('状态引用不存在或跨会话')
