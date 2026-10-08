from jsonschema import Draft202012Validator
from ..decision import GateDecision


class PostGuard:
    def __init__(self, catalog):
        self.catalog = catalog

    def check(self, name, result, context):
        entry = self.catalog.get(name)
        output = result.model_dump(mode="json")
        schema = entry["output_schema"]
        if result.ok and schema is not None:
            errors = list(Draft202012Validator(schema).iter_errors(result.data))
            if errors:
                return GateDecision(verdict="block", layer="post", reasons=("invalid_output",))
        for guard in entry["post_guards"]:
            decision = guard(result, context)
            if decision.verdict != "accept":
                return decision
            if decision.effective_value is not None:
                output = decision.effective_value
        return GateDecision(verdict="accept", layer="post", effective_value=output)

