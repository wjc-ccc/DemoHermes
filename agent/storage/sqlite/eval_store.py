"""Immutable evaluation standards and runs bound to their exact question hashes."""
from __future__ import annotations
import json
from .codec import milliseconds, dumps


class DatasetVersionConflictError(ValueError):
    pass


class SqliteEvalStore:
    def __init__(self, db):
        self.db = db

    def publish(self, dataset) -> bool:
        digest = dataset.fingerprint()
        with self.db.write() as conn:
            previous = conn.execute("SELECT content_hash FROM eval_datasets WHERE dataset_id=? AND dataset_version=?",
                                    (dataset.dataset_id, dataset.version)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise DatasetVersionConflictError("已发布案例版本不能覆盖；请发布新的 version")
                return False
            conn.execute("INSERT INTO eval_datasets (dataset_id,dataset_version,schema_version,title,description,source,"
                         "case_count,question_count,content_hash,dataset_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                dataset.dataset_id, dataset.version, dataset.schema_version, dataset.title,
                dataset.description, dataset.source, len(dataset.cases), sum(len(c.questions) for c in dataset.cases),
                digest, dataset.model_dump_json(exclude={"cases"}), milliseconds(dataset.created_at),
            ))
            for index, case in enumerate(dataset.cases):
                conn.execute("INSERT INTO eval_cases (dataset_id,dataset_version,case_id,ordinal,title,scenario,difficulty,"
                             "content_hash,case_json,target) VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    dataset.dataset_id, dataset.version, case.case_id, index, case.title, case.scenario, case.difficulty,
                    case.fingerprint(), case.model_dump_json(exclude={"questions"}), getattr(case, "target", "memory"),
                ))
                for ordinal, question in enumerate(case.questions):
                    conn.execute("INSERT INTO eval_questions (dataset_id,dataset_version,case_id,question_id,ordinal,text,"
                                 "asked_at,expected_action,requires_semantic_review,content_hash,question_json,"
                                 "question_schema_version,history_end_turn_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        dataset.dataset_id, dataset.version, case.case_id, question.question_id, ordinal, question.text,
                        milliseconds(question.asked_at) if question.asked_at else None, question.expected_action,
                        int(question.requires_semantic_review), question.fingerprint(), question.model_dump_json(),
                        getattr(question, "question_schema_version", "1"), getattr(question, "history_end_turn_id", None),
                    ))
        return True

    def get_dataset(self, dataset_id: str, version: str):
        from agent.types.eval.datasets import MemoryEvalDataset
        with self.db.read() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT dataset_json FROM eval_datasets WHERE dataset_id=? AND dataset_version=?",
                               (dataset_id, version)).fetchone()
            if not row:
                return None
            definition = json.loads(row[0])
            definition["cases"] = []
            for case_id, case_json in conn.execute(
                "SELECT case_id,case_json FROM eval_cases WHERE dataset_id=? AND dataset_version=? ORDER BY ordinal",
                (dataset_id, version),
            ):
                case = json.loads(case_json)
                case["questions"] = [json.loads(q[0]) for q in conn.execute(
                    "SELECT question_json FROM eval_questions WHERE dataset_id=? AND dataset_version=? AND case_id=? ORDER BY ordinal",
                    (dataset_id, version, case_id),
                )]
                definition["cases"].append(case)
        return MemoryEvalDataset.model_validate(definition)

    def list_datasets(self, dataset_id: str | None = None):
        with self.db.read() as conn:
            cursor = conn.execute("SELECT dataset_id,dataset_version,schema_version,title,case_count,question_count,"
                                  "content_hash,created_at FROM eval_datasets WHERE (? IS NULL OR dataset_id=?) "
                                  "ORDER BY created_at,dataset_id,dataset_version", (dataset_id, dataset_id))
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor]

    def start_run(self, *, run_id, dataset_id, dataset_version, provider, model, prompt_version,
                  extractor_version, details=None):
        with self.db.write() as conn:
            conn.execute("INSERT INTO eval_runs (run_id,dataset_id,dataset_version,provider,model,prompt_version,"
                         "extractor_version,started_at,run_json) VALUES (?,?,?,?,?,?,?,?,?)",
                         (run_id,dataset_id,dataset_version,provider,model,prompt_version,extractor_version,
                          milliseconds(),dumps(details or {})))

    def put_result(self, run_id, case_id, question_id, *, status, result):
        if status not in {"passed", "failed", "pending"}:
            raise ValueError("invalid evaluation result status")
        with self.db.write() as conn:
            question = conn.execute(
                "SELECT q.question_schema_version,q.content_hash FROM eval_questions q JOIN eval_runs r "
                "ON r.dataset_id=q.dataset_id AND r.dataset_version=q.dataset_version "
                "WHERE r.run_id=? AND q.case_id=? AND q.question_id=?", (run_id,case_id,question_id),
            ).fetchone()
            if not question:
                raise ValueError("result question is not in this run's published dataset")
            previous = conn.execute("SELECT result_json FROM eval_results WHERE run_id=? AND case_id=? AND question_id=?",
                                    (run_id,case_id,question_id)).fetchone()
            value = dict(result)
            if previous:
                old = json.loads(previous[0])
                if old.get("actual_answer") != value.get("actual_answer"):
                    raise ValueError("an actual answer cannot be rewritten during review")
                value["review_history"] = [*old.get("review_history", []),
                                           {key: val for key, val in old.items() if key != "review_history"}]
            conn.execute("INSERT INTO eval_results (run_id,case_id,question_id,question_schema_version,question_hash,"
                         "status,result_json) VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id,case_id,question_id) DO UPDATE SET "
                         "status=excluded.status,result_json=excluded.result_json",
                         (run_id,case_id,question_id,*question,status,dumps(value)))

    def finish_run(self, run_id, summary):
        with self.db.write() as conn:
            row = conn.execute("SELECT run_json FROM eval_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("unknown evaluation run")
            conn.execute("UPDATE eval_runs SET finished_at=?,run_json=? WHERE run_id=?",
                         (milliseconds(),dumps({**json.loads(row[0]), "summary": summary}),run_id))

    def get_run(self, run_id):
        with self.db.read() as conn:
            conn.execute("BEGIN")
            cursor = conn.execute("SELECT * FROM eval_runs WHERE run_id=?", (run_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            result = dict(zip((column[0] for column in cursor.description), row))
            result["details"] = json.loads(result.pop("run_json"))
            cursor = conn.execute("SELECT * FROM eval_results WHERE run_id=? ORDER BY case_id,question_id", (run_id,))
            names = [column[0] for column in cursor.description]
            result["results"] = [dict(zip(names, row)) for row in cursor]
            for question in result["results"]:
                question["result"] = json.loads(question.pop("result_json"))
        return result
