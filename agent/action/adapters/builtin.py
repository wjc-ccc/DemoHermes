"""适配现有 Tool，不绕过业务模块的最终权限/版本校验。"""
import json
from .resources import card_in_scope, resource_for
from agent.types.tools import ToolInvocationContext


class BuiltinToolAdapter:
    def __init__(self, tool, *, cards=None, manual=None, skills=None):
        self.tool = tool
        self.cards = cards
        self.manual = manual
        self.skills = skills

    def effective_args(self, arguments):
        values = dict(arguments)
        if self.tool.name == "memory_edit" and "item_id" in values:
            values["item_id"] = values["item_id"].strip().removeprefix("card:")
        if self.tool.name == "skill_manage":
            values["name"] = values.get("name", "").strip()
        return values

    def resource(self, arguments, context):
        return resource_for(self.tool.name, arguments, context, cards=self.cards, manual=self.manual, skills=self.skills)

    def invoke(self, arguments, context, *, idempotency_key):
        values = dict(arguments)
        before = self.resource(arguments, context)
        result = self.tool.execute(values, context=ToolInvocationContext(
            session_id=context.session_id, turn_id=context.turn_id, session_key=context.session_key,
            source_message_id=context.source_message_id, memory_scope=context.memory_scope, skills=context.skills,
        ))
        # 整段读前后版本一致才补充 read 的版本；不把新版本盖到旧内容上。
        if result.ok and result.data is not None and before == self.resource(arguments, context):
            if self.tool.name == "memory_read" and before[0] and before[1]:
                result = result.model_copy(update={"data": {**result.data, "revision": int(before[1])}})
                result.result_text = json.dumps(result.data, ensure_ascii=False)
        return result

    def observations(self, result, arguments, context):
        if not result.ok or not isinstance(result.data, dict):
            return ()
        if self.tool.name == "memory_search":
            return tuple((hit["reference"], str(hit["revision"])) for hit in result.data.get("hits", ())
                         if str(hit.get("reference", "")).startswith("card:") and hit.get("revision") is not None)
        if self.tool.name == "memory_read" and result.data.get("revision") is not None:
            return ((arguments["reference"], str(result.data["revision"])),)
        if self.tool.name == "use_skill" and result.data.get("skill_revision") is not None:
            return (("skill:" + arguments["skill_name"], str(result.data["skill_revision"])),)
        if self.tool.name == "memory_edit" and arguments.get("operation") == "manual_get":
            return (("manual:" + context.scope_key, result.data["hash"]),)
        return ()

