from agent.types.tools import ToolInvocationContext
from tests.loop_fakes import action_turn
import threading
import time
import uuid
import pytest
from agent.core.action.catalog import ActionToolCatalog
from agent.types.action.config import ActionConfig
from agent.types.action.policy import ToolPolicy, Toolset
from agent.core.action.runtime import ActionRuntime
from agent.core.action.adapters.builtin import BuiltinToolAdapter
from agent.core.action.storage.migrations import schema_directory
from agent.types.conversation import Session, Message
from agent.types.model import ToolCallRequest
from agent.tools.base import Tool
from agent.types.tools import ToolCallResult
from agent.storage.sqlite.runtime_storage import RuntimeStorage


class FakeBank:
    def __init__(self):
        self.receipts = {}
        self.calls = 0
        self.delay = 0
        self.lose_response = False
        self.version = "1"
        self.lock = threading.Lock()


class Pay(Tool):
    name = "fake_pay"
    description = "支付指定应付义务"
    parameters = {"type": "object", "properties": {
        "obligation_id": {"type": "string"}, "amount": {"type": "integer", "minimum": 1}},
        "required": ["obligation_id", "amount"]}

    def __init__(self, bank):
        self.bank = bank

    def execute(self, arguments, *, context):
        time.sleep(self.bank.delay)
        with self.bank.lock:
            key = arguments["_idempotency_key"]
            if key not in self.bank.receipts:
                self.bank.calls += 1
                self.bank.receipts[key] = {"external_ref": "bank:" + key, "amount": arguments["amount"]}
            receipt = self.bank.receipts[key]
        if self.bank.lose_response:
            raise RuntimeError("银行已入账，但响应丢失")
        return ToolCallResult(data=receipt, result_text="已入账")


class Query(Tool):
    name = "payment_query"
    description = "查询付款外部状态"
    parameters = {"type": "object", "properties": {"idempotency_key": {"type": "string"}},
                  "required": ["idempotency_key"]}

    def __init__(self, bank):
        self.bank = bank

    def execute(self, arguments, *, context):
        receipt = self.bank.receipts.get(arguments["idempotency_key"])
        return ToolCallResult(data={"status": "succeeded", "result": receipt} if receipt else {"status": "not_found"})


class BankAdapter(BuiltinToolAdapter):
    def resource(self, arguments, context):
        if self.tool.name == "fake_pay":
            return "payroll:" + arguments["obligation_id"], self.tool.bank.version
        return None, None

    def invoke(self, arguments, context, *, idempotency_key):
        return self.tool.execute({**arguments, "_idempotency_key": idempotency_key}, context=ToolInvocationContext())


@pytest.fixture
def bank_runtime(tmp_path):
    bank = FakeBank()
    storage = RuntimeStorage.open(tmp_path / "agent.db", extra_schema_dirs=(schema_directory(),))
    catalog = ActionToolCatalog()
    pay, query = Pay(bank), Query(bank)
    policy = ToolPolicy(when_to_use="付款", when_not_to_use="未审批或义务不明确时", effect="external", risk="high",
                        permission="user", idempotency="keyed", key_fields=("obligation_id",), recovery_tool=query.name)
    catalog.register(pay, policy, BankAdapter(pay))
    catalog.register(query, ToolPolicy(when_to_use="查单", when_not_to_use="付款"), BankAdapter(query))
    config = ActionConfig(toolsets=(Toolset(tools=catalog.names(), max_risk="high"),), lease_ms=20)
    runtime = ActionRuntime(storage.db, catalog, config=config)
    yield bank, storage, runtime
    runtime.stop()


def context(runtime, *, turn_id=None):
    return action_turn(runtime, Session(session_id="s", author_id="u"), Message(message_id=uuid.uuid4().hex),
                              turn_id=turn_id or uuid.uuid4().hex)


def payment(**changes):
    return ToolCallRequest(id=uuid.uuid4().hex, name="fake_pay", arguments={"obligation_id": "E0007", "amount": 100, **changes})
