import uuid
from ..chain.chain import Artifact
from ..ledger import now_ms, digest


class SqliteArtifactStore:
    def __init__(self, database):
        self.db = database

    def put(self, artifact):
        if digest(artifact.body) != artifact.content_hash:
            raise ValueError("artifact_hash_mismatch")
        with self.db.write() as conn:
            conn.execute("INSERT INTO action_artifacts VALUES (?,?,?,?,?,?,?,?,?,?)", (
                artifact.artifact_id, artifact.scope_key, artifact.plan_id, artifact.step_id, artifact.segment_id,
                artifact.status, artifact.content_hash, artifact.input_hash, artifact.created_at, artifact.model_dump_json()))
        return artifact

    def get(self, artifact_id):
        with self.db.read() as conn:
            row = conn.execute("SELECT payload_json FROM action_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        return Artifact.model_validate_json(row[0]) if row else None

    def read_accepted(self, artifact_id, content_hash, scope_key):
        artifact = self.get(artifact_id)
        if (not artifact or artifact.scope_key != scope_key or artifact.status != "accepted"
            or artifact.content_hash != content_hash or digest(artifact.body) != content_hash):
            raise ValueError("artifact_not_accepted_or_hash_changed")
        return artifact

    def latest(self, plan_id, step_id, segment_id, input_hash):
        with self.db.read() as conn:
            row = conn.execute("SELECT payload_json FROM action_artifacts WHERE plan_id=? AND step_id=? "
                "AND segment_id=? AND input_hash=? ORDER BY rowid DESC LIMIT 1", (plan_id, step_id, segment_id, input_hash)).fetchone()
        return Artifact.model_validate_json(row[0]) if row else None

    def decide(self, artifact, decision, fingerprint):
        updated = artifact.model_copy(update={"status": "accepted" if decision.verdict == "accept" else "rejected",
                                               "gate_fingerprint": fingerprint})
        with self.db.write() as conn:
            conn.execute("INSERT INTO action_gate_decisions VALUES (?,?,?,?,?,?)", (
                uuid.uuid4().hex, artifact.artifact_id, fingerprint, decision.verdict, now_ms(), decision.model_dump_json()))
            conn.execute("UPDATE action_artifacts SET status=?,payload_json=? WHERE artifact_id=? AND content_hash=?",
                         (updated.status, updated.model_dump_json(), artifact.artifact_id, artifact.content_hash))
        return updated
