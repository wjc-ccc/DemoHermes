"""四层确定性验收；开放语义需要调用方注册任务专用检查。"""
import json
from jsonschema import Draft202012Validator
from agent.types.action.execution import GateDecision, digest


class ChainGates:
    def __init__(self, database, ledger, artifacts):
        self.db, self.ledger, self.artifacts = database, ledger, artifacts
        self._checks = {}

    def register(self, name, check, *, layer="semantics", version="1"):
        if layer not in {"semantics", "facts"}:
            raise ValueError("任务检查必须属于 semantics/facts")
        self._checks[name] = (check, layer, version)

    def fingerprint(self, segment):
        return digest({"segment": segment.model_dump(mode="json"), "builtin": "1",
            "checks": {name: self._checks[name][2] if name in self._checks else "missing"
                       for name in (*segment.semantic_gates, *segment.fact_gates)}})

    def parse(self, content, segment):
        if segment.output_schema.get("type") in {"object", "array", "number", "integer", "boolean", "null"}:
            return json.loads(content)
        return content

    def check(self, artifact, segment):
        if digest(artifact.body) != artifact.content_hash:
            return GateDecision(verdict="block", layer="syntax", reasons=("artifact_hash_mismatch",))
        errors = list(Draft202012Validator(segment.output_schema).iter_errors(artifact.body))
        if errors:
            return GateDecision(verdict="block", layer="structure", reasons=("artifact_schema_invalid",))
        for layer, names in (("semantics", segment.semantic_gates), ("facts", segment.fact_gates)):
            for name in names:
                registered = self._checks.get(name)
                if not registered or registered[1] != layer:
                    return GateDecision(verdict="block", layer=layer, reasons=("validator_not_registered:" + name,))
                try:
                    decision = registered[0](artifact)
                except Exception as exc:
                    return GateDecision(verdict="block", layer=layer, reasons=("validator_error:" + name,), detail=str(exc))
                if not isinstance(decision, GateDecision):
                    return GateDecision(verdict="block", layer=layer, reasons=("invalid_gate_decision:" + name,))
                if decision.verdict != "accept":
                    return decision
        if not artifact.source_refs:
            return GateDecision(verdict="block", layer="facts", reasons=("missing_sources",))
        for ref in artifact.source_refs:
            if ref.startswith("turn:"):
                with self.db.read() as conn:
                    valid = conn.execute("SELECT 1 FROM turns t JOIN sessions s ON s.session_id=t.session_id "
                        "WHERE t.turn_id=? AND s.scope_key=? AND t.status='completed'", (ref[5:], artifact.scope_key)).fetchone()
                if not valid:
                    return GateDecision(verdict="block", layer="facts", reasons=("turn_not_committed",))
            elif ref.startswith("ledger:"):
                item = self.ledger.get(ref[7:])
                if not item or item.scope_key != artifact.scope_key or item.effect_state != "succeeded" or item.publish_state == "blocked":
                    return GateDecision(verdict="block", layer="facts", reasons=("source_not_publishable",))
            elif ref.startswith("artifact:"):
                try:
                    _, identity, content_hash = ref.split(":", 2)
                    self.artifacts.read_accepted(identity, content_hash, artifact.scope_key)
                except ValueError:
                    return GateDecision(verdict="block", layer="facts", reasons=("source_artifact_invalid",))
            else:
                return GateDecision(verdict="block", layer="facts", reasons=("unverified_source",))
        return GateDecision(verdict="accept", layer="facts", evidence_refs=artifact.source_refs)
