"""银行独立数据库已提交后直接终止进程，验证真实 crash window。"""
from agent.types.tools import ToolInvocationContext
from tests.loop_fakes import action_turn
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid
from agent.core.action.catalog import ActionToolCatalog
from agent.types.action.config import ActionConfig
from agent.types.action.policy import ToolPolicy, Toolset
from agent.core.action.runtime import ActionRuntime
from agent.core.action.adapters.builtin import BuiltinToolAdapter
from agent.core.action.storage.migrations import schema_directory
from agent.types.conversation import Session, Message
from agent.types.model import ToolCallRequest
from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.core.tools.base import Tool
from agent.types.tools import ToolCallResult


class DurablePay(Tool):
    name = "pay"
    description = "持久模拟付款"
    parameters = {"type": "object", "properties": {"obligation_id": {"type": "string"}}, "required": ["obligation_id"]}
    def __init__(self, bank_path, crash):
        self.bank_path, self.crash = bank_path, crash
    def execute(self, arguments, *, context):
        key = arguments["_idempotency_key"]
        with sqlite3.connect(self.bank_path) as conn:
            conn.execute("INSERT OR IGNORE INTO payments VALUES (?,?)", (key, arguments["obligation_id"]))
        if self.crash:
            os._exit(7)
        return ToolCallResult(data={"external_ref": "bank:" + key})


class DurableQuery(Tool):
    name = "query"
    description = "查询持久银行"
    parameters = {"type": "object", "properties": {"idempotency_key": {"type": "string"}}, "required": ["idempotency_key"]}
    def __init__(self, bank_path):
        self.bank_path = bank_path
    def execute(self, arguments, *, context):
        with sqlite3.connect(self.bank_path) as conn:
            found = conn.execute("SELECT 1 FROM payments WHERE payment_key=?", (arguments["idempotency_key"],)).fetchone()
        return ToolCallResult(data={"status": "succeeded", "result": {"external_ref": "bank:" + arguments["idempotency_key"]}} if found
                              else {"status": "not_found"})


class DurableAdapter(BuiltinToolAdapter):
    def invoke(self, arguments, context, *, idempotency_key):
        return self.tool.execute({**arguments, "_idempotency_key": idempotency_key}, context=ToolInvocationContext())


def durable_runtime(agent_path, bank_path, *, crash=False):
    with sqlite3.connect(bank_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS payments(payment_key TEXT PRIMARY KEY, obligation TEXT)")
    storage = RuntimeStorage.open(agent_path, extra_schema_dirs=(schema_directory(),))
    catalog = ActionToolCatalog()
    pay, query = DurablePay(bank_path, crash), DurableQuery(bank_path)
    policy = ToolPolicy(when_to_use="发薪", when_not_to_use="没有业务义务时", effect="external", risk="high",
                        idempotency="keyed", key_fields=("obligation_id",), recovery_tool="query")
    catalog.register(pay, policy, DurableAdapter(pay))
    catalog.register(query, ToolPolicy(when_to_use="查单", when_not_to_use="付款"), DurableAdapter(query))
    return ActionRuntime(storage.db, catalog, config=ActionConfig(toolsets=(Toolset(tools=catalog.names(), max_risk="high"),)))


def test_crash_after_external_commit_is_reconciled_without_second_payment(tmp_path):
    agent_path, bank_path = tmp_path / "agent.db", tmp_path / "bank.db"
    script = """
import sys
from test_crash_recovery import durable_runtime
from agent.types.conversation import Session, Message
from agent.types.model import ToolCallRequest
runtime = durable_runtime(sys.argv[1], sys.argv[2], crash=True)
from tests.loop_fakes import action_turn
context = action_turn(runtime, Session(author_id='u'), Message(), turn_id='crashing-turn')
runtime.handler.handle_batch((ToolCallRequest(id='crash-call', name='pay', arguments={'obligation_id': 'E0007'}),), context)
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(Path(__file__).parent), str(Path(__file__).resolve().parents[2])))
    child = subprocess.run([sys.executable, "-c", script, str(agent_path), str(bank_path)],
                           env=environment, capture_output=True, timeout=20)
    assert child.returncode == 7, child.stderr.decode(errors="replace")
    restarted = durable_runtime(agent_path, bank_path)
    try:
        item = restarted.ledger.list()[0]
        assert item.effect_state == "started"
        restarted.ledger.update(item, changes={"lease_expires_at": 0}, event_type="lease_expired")
        assert restarted.recover()[0].effect_state == "succeeded"
        ctx = action_turn(restarted, Session(author_id="u"), Message(), turn_id="restarted-turn")
        replay = restarted.handler.handle_batch((ToolCallRequest(id=uuid.uuid4().hex, name="pay", arguments={"obligation_id": "E0007"}),), ctx)[0]
        assert replay.ok
        with sqlite3.connect(bank_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
    finally:
        restarted.stop()
