"""Memory 评测考卷的版本化存储。

职责：在事务内发布版本，按版本读取案例与黄金问题，拒绝同版不同内容。
边界：保存的是评测标准，不是用户卡片；模型记忆工具不读取这些表。
关联：RuntimeStorage 提供实例；eval/datasets/memory_dataset.py 导入和查询。
"""
from __future__ import annotations

import json
from .codec import milliseconds


class DatasetVersionConflictError(ValueError):
    """已发布版本的考题或答案发生变化，需发布新的内容版本。"""


class SqliteMemoryEvalStore:
    def __init__(self, db):
        self.db = db

    def publish(self, dataset) -> bool:
        digest = dataset.fingerprint()
        with self.db.write() as connection:
            previous = connection.execute(
                "SELECT content_hash FROM memory_eval_datasets WHERE dataset_id=? AND version=?",
                (dataset.dataset_id, dataset.version),
            ).fetchone()
            if previous:
                if previous[0] != digest:
                    raise DatasetVersionConflictError("已发布案例版本不能覆盖；请使用新的 version")
                return False
            connection.execute("INSERT INTO memory_eval_datasets VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                dataset.dataset_id, dataset.version, dataset.schema_version, dataset.title,
                dataset.description, dataset.source, len(dataset.cases),
                sum(len(case.questions) for case in dataset.cases), digest,
                dataset.model_dump_json(exclude={"cases"}), milliseconds(dataset.created_at),
            ))
            for index, case in enumerate(dataset.cases):
                connection.execute("INSERT INTO memory_eval_cases VALUES (?,?,?,?,?,?,?,?,?)", (
                    dataset.dataset_id, dataset.version, case.case_id, index, case.title,
                    case.scenario, case.difficulty, case.fingerprint(),
                    case.model_dump_json(exclude={"questions"}),
                ))
                for ordinal, question in enumerate(case.questions):
                    connection.execute("INSERT INTO memory_eval_questions VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                        dataset.dataset_id, dataset.version, case.case_id, question.question_id, ordinal,
                        question.text, milliseconds(question.asked_at) if question.asked_at else None,
                        question.expected_action, int(question.requires_semantic_review),
                        question.fingerprint(), question.model_dump_json(),
                    ))
        return True

    def get_dataset(self, dataset_id: str, version: str):
        # 在方法内导入领域类型，避免 RuntimeStorage 与 eval 包装配产生循环依赖。
        from ...eval.datasets.types.memory_eval_types import MemoryEvalDataset
        with self.db.read() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT payload_json FROM memory_eval_datasets WHERE dataset_id=? AND version=?",
                (dataset_id, version),
            ).fetchone()
            if not row:
                return None
            definition = json.loads(row[0])
            cases = []
            for case_id, payload in connection.execute(
                "SELECT case_id,payload_json FROM memory_eval_cases WHERE dataset_id=? AND dataset_version=? ORDER BY ordinal",
                (dataset_id, version),
            ):
                case = json.loads(payload)
                case["questions"] = [json.loads(q[0]) for q in connection.execute(
                    "SELECT payload_json FROM memory_eval_questions WHERE dataset_id=? AND dataset_version=? AND case_id=? ORDER BY ordinal",
                    (dataset_id, version, case_id),
                )]
                cases.append(case)
            definition["cases"] = cases
        return MemoryEvalDataset.model_validate(definition)

    def list_datasets(self, dataset_id: str):
        with self.db.read() as connection:
            cursor = connection.execute(
                "SELECT dataset_id,version,schema_version,title,case_count,question_count,content_hash,created_at "
                "FROM memory_eval_datasets WHERE dataset_id=? ORDER BY created_at,version", (dataset_id,),
            )
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor]
