"""Turn 与不可变消息/工具事实。提交时机仍由现有 Gateway 决定。"""
from agent.types.conversation import Message, ToolCall
import json
from agent.types.memory.source import TurnRecord
from .codec import milliseconds, dumps
from .errors import IntegrityError, AuditConflictError

def insert_immutable(conn, table, column, identity, payload, sql, values):
    fact_column = {'messages':'message_json','tool_calls':'tool_call_json'}[table]
    row = conn.execute(f'SELECT {fact_column} FROM {table} WHERE {column}=?',(identity,)).fetchone()
    if row:
        model = Message if table == 'messages' else ToolCall
        if row[0] != payload and model.model_validate_json(row[0]).model_dump_json() != payload:
            raise AuditConflictError(f'{table}.{column}={identity} 已存在且 payload 不同')
        return
    conn.execute(sql,values)

def insert_turn(conn, turn):
    payload = turn.model_dump_json()
    row = conn.execute('SELECT sequence_no,turn_json FROM turns WHERE turn_id=?',(turn.turn_id,)).fetchone()
    if row:
        # 命名迁移可能改变 JSON 字段顺序；不可变性按完整事实内容校验。
        if row[1] != payload and json.loads(row[1]) != json.loads(payload):
            raise AuditConflictError('Turn payload 冲突')
        return row[0]
    sequence = conn.execute('SELECT COALESCE(MAX(sequence_no),-1)+1 FROM turns WHERE session_id=?',
                            (turn.session_id,)).fetchone()[0]
    status = 'interrupted' if turn.interrupted else ('completed' if turn.completed else 'failed')
    conn.execute('INSERT INTO turns (turn_id,session_id,sequence_no,status,started_at,finished_at,'
                 'error_text,turn_json,created_at,input_token_count,output_token_count) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                 (turn.turn_id,turn.session_id,sequence,status,milliseconds(turn.started_at),
                  milliseconds(turn.finished_at),turn.error,payload,milliseconds(turn.finished_at),
                  turn.input_token_count,turn.output_token_count))
    return sequence

def insert_session_facts(conn, session, turn):
    turn_ids = {turn.user_message_id,*turn.assistant_message_ids} if turn else set()
    tool_ids = set(turn.tool_call_ids) if turn else set()
    for message in session.messages:
        if message.session_id != session.session_id:
            raise IntegrityError('message 属于其他会话')
        if message.metadata.get('origin') == 'gateway_error':
            source_type = 'gateway_generated'
        elif message.metadata.get('origin') == 'failed_turn':
            source_type = 'failed_turn_partial'
        else:
            source_type = {'user':'user_authored','assistant':'assistant_authored',
                           'tool':'tool_observed','system':'system_injected'}.get(message.role,'system_injected')
        linked = bool(turn and (message.message_id in turn_ids or set(message.tool_call_ids) & tool_ids))
        payload = message.model_dump_json()
        insert_immutable(conn,'messages','message_id',message.message_id,payload,
            'INSERT INTO messages (message_id,session_id,turn_id,seq_id,role,content_text,source_type,created_at,message_json) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (message.message_id,session.session_id,turn.turn_id if linked else None,
             message.seq_id,message.role,message.content_text,source_type,milliseconds(message.created_at),payload))
    for call in session.tool_calls:
        if call.session_id and call.session_id != session.session_id:
            raise IntegrityError('tool call 属于其他会话')
        payload = call.model_dump_json()
        insert_immutable(conn,'tool_calls','tool_call_id',call.tool_call_id,payload,
            'INSERT INTO tool_calls (tool_call_id,session_id,turn_id,trajectory_id,tool_name,status,'
            'created_at,tool_call_json) VALUES (?,?,?,?,?,?,?,?)',
            (call.tool_call_id,session.session_id,turn.turn_id if turn and call.tool_call_id in tool_ids else None,
             call.trajectory_id,call.tool_name,call.status,milliseconds(call.created_at),payload))

class SqliteExecutionStore:
    def __init__(self, database):
        self.db = database

    def get_record(self, turn_id):
        with self.db.read() as conn:
            row = conn.execute('SELECT turn_json FROM turns WHERE turn_id=?',(turn_id,)).fetchone()
            return TurnRecord.model_validate_json(row[0]) if row else None

    def list_turns(self, session_id, *, limit=100, offset=0):
        """Return turn summaries; payload details stay behind get_turn."""
        with self.db.read() as conn:
            rows = conn.execute('SELECT t.turn_id,t.sequence_no,t.status,t.started_at,t.finished_at,'
                't.error_text,(SELECT COUNT(*) FROM messages m WHERE m.turn_id=t.turn_id),'
                '(SELECT COUNT(*) FROM tool_calls c WHERE c.turn_id=t.turn_id),t.turn_json '
                'FROM turns t WHERE t.session_id=? ORDER BY t.sequence_no DESC LIMIT ? OFFSET ?',
                (session_id,min(100,max(1,limit)),max(0,offset))).fetchall()
        result = []
        for row in rows:
            trace = json.loads(row[8]).get('perception_trace') or {}
            result.append(dict(turn_id=row[0],sequence_no=row[1],status=row[2],started_at=row[3]/1000,
                finished_at=row[4]/1000,error=row[5],message_count=row[6],tool_call_count=row[7],
                has_perception_trace=bool(trace),perception_metrics=trace.get('metrics')))
        return result

    def get_turn(self, turn_id):
        """Return one turn plus its exact messages, tools and Perception trace."""
        with self.db.read() as conn:
            conn.execute('BEGIN')
            row = conn.execute('SELECT turn_id,session_id,sequence_no,status,started_at,finished_at,'
                               'error_text,turn_json,environment_json FROM turns WHERE turn_id=?',(turn_id,)).fetchone()
            if not row:
                return None
            messages = [json.loads(r[0]) for r in conn.execute(
                'SELECT message_json FROM messages WHERE turn_id=? ORDER BY seq_id',(turn_id,))]
            calls = [ToolCall.model_validate_json(r[0]).observation_payload() for r in conn.execute(
                'SELECT tool_call_json FROM tool_calls WHERE turn_id=? ORDER BY created_at,tool_call_id',(turn_id,))]
        return dict(turn_id=row[0],session_id=row[1],sequence_no=row[2],status=row[3],
                    started_at=row[4]/1000,finished_at=row[5]/1000,error=row[6],
                    messages=messages,tool_calls=calls,perception_trace=json.loads(row[7]).get('perception_trace'),
                    environment=json.loads(row[8]) if row[8] else None)

    def put_environment(self, snapshot):
        payload = snapshot.model_dump_json()
        with self.db.write() as conn:
            row = conn.execute('SELECT session_id,environment_json FROM turns WHERE turn_id=?',
                               (snapshot.turn_id,)).fetchone()
            if not row or row[0] != snapshot.session_id:
                raise IntegrityError('环境快照必须属于已提交的轮次')
            if row[1] is not None and row[1] != payload:
                raise AuditConflictError('此轮环境快照已存在')
            conn.execute('UPDATE turns SET environment_json=? WHERE turn_id=?',(payload,snapshot.turn_id))

    def get_environment(self, snapshot_id):
        with self.db.read() as conn:
            row = conn.execute("SELECT environment_json FROM turns WHERE "
                               "json_extract(environment_json,'$.snapshot_id')=?",(snapshot_id,)).fetchone()
            return json.loads(row[0]) if row else None
