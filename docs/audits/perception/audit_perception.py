"""Inventory Perception symbols without importing application code or reading .env.

Run from the repository root:
    .venv/Scripts/python.exe docs/audits/perception/audit_perception.py

References are matched by symbol name, not inferred receiver type. A missing
explicit read is a review candidate, never proof that a serialized field is dead.
"""
from __future__ import annotations

import ast
import hashlib
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PERCEPTION = "agent/core/perception/"
SHARED_MODELS = {
    "agent/types/context.py": {"AnchorFact": None, "Anchor": None, "AdmissionVerdict": None,
                               "ContextBudget": None, "TokenEstimator": None},
    "agent/types/conversation.py": {"Session": {"summary", "context_anchor", "context_verdicts"}},
    "agent/types/perception/result.py": {"PerceptionRecord": None, "PerceptionMetrics": None, "PerceptionResult": None},
    "agent/types/perception/admission.py": {"AdmissionBand": None, "AdmissionPolicy": None},
    "agent/types/perception/judge.py": {"ContextJudgeRequest": None, "ContextJudgeResult": None, "ContextJudge": None},
    "agent/types/perception/trace.py": {"PerceptionBuildSnapshot": None, "PerceptionTraceSnapshot": None},
    "agent/types/memory/document.py": {"DocumentRead": None},
}

SAMPLE_LIMIT = 8


def location(path: str, node: ast.AST) -> str:
    return f"{path}:{node.lineno}"


def bucket(path: str) -> str:
    return "tests" if path.startswith("tests/") else "production"


def meaningful_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    return body


def main() -> None:
    references = defaultdict(lambda: {"production": set(), "tests": set()})
    reads = defaultdict(lambda: {"production": set(), "tests": set()})
    writes = defaultdict(lambda: {"production": set(), "tests": set()})
    imports = defaultdict(list)
    trees = {}
    parse_errors = []
    serialization_sites = []
    source_hashes = {}
    for directory in ("agent", "tests"):
        for source in sorted((ROOT / directory).rglob("*.py")):
            path = source.relative_to(ROOT).as_posix()
            try:
                raw = source.read_bytes()
                tree = ast.parse(raw.decode("utf-8-sig"), filename=path)
            except (OSError, SyntaxError, UnicodeError) as exc:
                parse_errors.append({"path": path, "error": str(exc)})
                continue
            trees[path] = tree
            if path.startswith(PERCEPTION) or path in SHARED_MODELS:
                source_hashes[path] = hashlib.sha256(raw).hexdigest()
            group = bucket(path)
            for node in ast.walk(tree):
                at = location(path, node) if hasattr(node, "lineno") else path
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    references[node.id][group].add(at)
                elif isinstance(node, ast.Attribute):
                    references[node.attr][group].add(at)
                    target = reads if isinstance(node.ctx, ast.Load) else writes
                    target[node.attr][group].add(at)
                elif isinstance(node, ast.keyword) and node.arg:
                    writes[node.arg][group].add(at)
                elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    target = reads if isinstance(node.ctx, ast.Load) else writes
                    target[node.slice.value][group].add(at)
                elif isinstance(node, ast.Call):
                    callee = node.func
                    name = callee.attr if isinstance(callee, ast.Attribute) else callee.id if isinstance(callee, ast.Name) else ""
                    argument = node.args[1] if name in {"getattr", "hasattr", "setattr"} and len(node.args) > 1 else node.args[0] if name == "get" and node.args else None
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        target = writes if name == "setattr" else reads
                        target[argument.value][group].add(at)
                        references[argument.value][group].add(at)
                    if name in {"model_dump", "model_dump_json", "event_data"} and group == "production":
                        serialization_sites.append(at)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        imports[path].append({"name": alias.asname or alias.name.split(".")[0], "at": at})

    functions = []
    models = []
    unused_import_candidates = []
    for path, tree in trees.items():
        is_perception = path.startswith(PERCEPTION)
        if not is_perception and path not in SHARED_MODELS:
            continue
        for owner in tree.body:
            members = owner.body if isinstance(owner, ast.ClassDef) else [owner]
            for node in members:
                if is_perception and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = meaningful_body(node)
                    decorators = [ast.unparse(item) for item in node.decorator_list]
                    function = {
                        "symbol": f"{owner.name}.{node.name}" if isinstance(owner, ast.ClassDef) else node.name,
                        "defined_at": location(path, node),
                        "decorators": decorators,
                        "framework_or_protocol": node.name.startswith("__") or any("validator" in item for item in decorators) or any(isinstance(base, ast.Name) and base.id == "Protocol" for base in getattr(owner, "bases", [])),
                        "constant_return": len(body) == 1 and isinstance(body[0], ast.Return) and (body[0].value is None or isinstance(body[0].value, ast.Constant)),
                    }
                    for group in ("production", "tests"):
                        refs = sorted(references[node.name][group])
                        function[f"{group}_reference_count"] = len(refs)
                        function[f"{group}_reference_samples"] = refs[:SAMPLE_LIMIT]
                    functions.append(function)
            if not isinstance(owner, ast.ClassDef):
                continue
            if not is_perception and owner.name not in SHARED_MODELS[path]:
                continue
            selected = None if is_perception else SHARED_MODELS[path][owner.name]
            fields = []
            for node in owner.body:
                if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
                    continue
                name = node.target.id
                if name == "model_config" or selected is not None and name not in selected:
                    continue
                item = {"name": name, "annotation": ast.unparse(node.annotation), "defined_at": location(path, node)}
                for label, index in (("explicit_reads", reads), ("writes", writes)):
                    for group in ("production", "tests"):
                        matches = sorted(index[name][group])
                        item[f"{group}_{label}_count"] = len(matches)
                        item[f"{group}_{label}_samples"] = matches[:SAMPLE_LIMIT]
                fields.append(item)
            if fields:
                models.append({"model": owner.name, "defined_at": location(path, owner), "fields": fields})
        if is_perception:
            local_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
            for item in imports[path]:
                if item["name"] not in local_names and item["name"] != "annotations" and not path.endswith("/__init__.py"):
                    unused_import_candidates.append(item)

    candidates = [item for item in functions if not item["production_reference_count"] and not item["framework_or_protocol"]]
    definitions = defaultdict(list)
    for model in models:
        definitions[model["model"]].append(model["defined_at"])
    duplicate_names = {name: sites for name, sites in definitions.items() if len(sites) > 1}
    output = {
        "scope": [PERCEPTION, *SHARED_MODELS],
        "limitations": [
            "Symbol-name matches can conflate different receiver types; inspect every candidate.",
            "Pydantic validators, protocol declarations and dunder methods have implicit callers.",
            "model_dump/event_data, JSON storage, metrics.items and generic frontend tables consume fields implicitly.",
            "References and test coverage do not prove reachability from the production entry point.",
            "The inventory does not prove a field changes behavior or validate design-document claims.",
        ],
        "scanned_python_files": len(trees),
        "parse_errors": parse_errors,
        "target_source_sha256": source_hashes,
        "functions": functions,
        "models": models,
        "no_production_reference_candidates": candidates,
        "constant_return_functions": [item for item in functions if item["constant_return"]],
        "unused_import_candidates": unused_import_candidates,
        "duplicate_model_names": duplicate_names,
        "production_serialization_sites": sorted(set(serialization_sites)),
    }
    destination = Path(__file__).with_name("perception_inventory.json")
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(destination),
        "scanned_python_files": len(trees),
        "parse_errors": len(parse_errors),
        "perception_function_count": len(functions),
        "inventory_model_count": len(models),
        "inventory_field_count": sum(len(model["fields"]) for model in models),
        "no_production_reference_candidates": [item["symbol"] for item in candidates],
        "constant_return_functions": [item["symbol"] for item in functions if item["constant_return"]],
        "unused_import_candidates": unused_import_candidates,
        "duplicate_model_names": duplicate_names,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
