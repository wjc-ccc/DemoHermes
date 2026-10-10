"""可信工具注册与启动检验；MCP 声明本身不授予执行权限。"""
from copy import deepcopy
from types import CodeType
from jsonschema import Draft202012Validator
from agent.types.action.execution import digest
from .policy import RISK_ORDER


def strict_schema(schema):
    schema = deepcopy(schema)
    def visit(node):
        if isinstance(node, dict):
            if "$ref" in node and not str(node["$ref"]).startswith("#/"):
                raise ValueError("工具 schema 禁止远程 $ref")
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)
    Draft202012Validator.check_schema(schema)
    return schema


class ActionToolCatalog:
    def __init__(self):
        self._entries = {}

    def register(self, tool, policy, adapter, *, operation_policies=None, output_schema=None, pre_guards=(), post_guards=()):
        if tool.name in self._entries:
            raise ValueError(f"重复注册 action 工具: {tool.name}")
        self._entries[tool.name] = {
            "tool": tool, "policy": policy, "adapter": adapter,
            "operations": operation_policies or {}, "output_schema": output_schema,
            "post_guards": tuple(post_guards),
            "pre_guards": tuple(pre_guards),
        }
        strict_schema(tool.parameters)
        if output_schema is not None:
            Draft202012Validator.check_schema(output_schema)

    def names(self):
        return tuple(self._entries)

    def get(self, name):
        return self._entries.get(name)

    def policy_for(self, name, arguments):
        entry = self._entries[name]
        return entry["operations"].get(arguments.get("operation") or arguments.get("action"), entry["policy"])

    def contract(self, name):
        entry = self._entries[name]
        return {
            "name": name, "description": entry["tool"].description,
            "parameters": strict_schema(entry["tool"].parameters),
            "output_schema": entry["output_schema"], "policy": entry["policy"].model_dump(mode="json"),
            "operations": {key: policy.model_dump(mode="json") for key, policy in entry["operations"].items()},
            "implementation_hash": self._code_hash(entry["tool"].execute),
            "pre_guards": [{"version": getattr(guard, "version", "1"), "code_hash": self._code_hash(guard)}
                           for guard in entry["pre_guards"]],
            "post_guards": [{"version": getattr(guard, "version", "1"), "code_hash": self._code_hash(guard)}
                            for guard in entry["post_guards"]],
        }

    @staticmethod
    def _code_hash(function):
        code = getattr(function, "__code__", None)
        def constant(value):
            if isinstance(value, CodeType):
                return {"bytecode": value.co_code.hex(), "names": value.co_names,
                        "constants": tuple(constant(item) for item in value.co_consts)}
            if isinstance(value, (tuple, frozenset)):
                values = [constant(item) for item in value]
                return sorted(values, key=repr) if isinstance(value, frozenset) else values
            return repr(value)
        return digest(constant(code)) if code else None

    def contract_hash(self, name):
        return digest(self.contract(name))

    def validate(self, toolset):
        for name in toolset.tools:
            entry = self.get(name)
            if entry is None:
                raise ValueError(f"工具未注册: {name}")
            policies = (entry["policy"], *entry["operations"].values())
            for policy in policies:
                if RISK_ORDER[policy.risk] > RISK_ORDER[toolset.max_risk]:
                    raise ValueError(f"工具风险超出工具集: {name}")
                if toolset.max_calls.get(name, policy.max_calls_per_turn) not in range(1, policy.max_calls_per_turn + 1):
                    raise ValueError(f"工具集不能放宽调用预算: {name}")
                if RISK_ORDER[policy.risk] >= RISK_ORDER["high"] and policy.effect != "read":
                    if not policy.recovery_tool or policy.recovery_tool not in toolset.tools:
                        raise ValueError(f"高风险工具缺少可用 recovery_tool: {name}")
                    recovery = self.policy_for(policy.recovery_tool, {})
                    if recovery.effect != "read":
                        raise ValueError("recovery_tool 必须只读")

    def schemas(self, context):
        result = []
        for name in context.toolset.tools:
            contract = context.contracts.get(name) or self.contract(name)
            policy = self._entries[name]["policy"]
            result.append({"type": "function", "function": {
                "name": name,
                "description": contract["description"] + f"\n何时使用：{policy.when_to_use}\n何时不用：{policy.when_not_to_use}",
                "parameters": contract["parameters"],
            }})
        return result

