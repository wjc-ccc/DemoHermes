"""通用作业队列；领取凭证与完成标记构成原子提交的幂等入口。"""
import hashlib
import json
import uuid
from .codec import dumps, milliseconds
from .errors import LeaseLostError, RevisionConflictError, IntegrityError
from .types import Job

_FIELDS = ('id,job_type,handler_version,session_id,turn_id,dedupe_key,input_hash,input_json,'
           'status,revision,attempt_count,created_at,available_at,lease_owner,lease_token,'
           'lease_expires_at,last_error,result_json')

def read_job(conn, job_id):
    row = conn.execute('SELECT '+_FIELDS+' FROM jobs WHERE id=?',(job_id,)).fetchone()
    return _decode(row) if row else None

def _decode(row):
    return Job(*row[:7],json.loads(row[7]),*row[8:17],json.loads(row[17]) if row[17] else None)

def enqueue_job(conn, *, job_type, handler_version, input, dedupe_key, session_id=None,
                turn_id=None, job_id=None, available_at=None):
    payload = dumps(input)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    existing = conn.execute('SELECT id FROM jobs WHERE dedupe_key=?',(dedupe_key,)).fetchone()
    if existing:
        job = read_job(conn,existing[0])
        if (job.job_type,job.handler_version,job.input_hash,job.session_id,job.turn_id) != (
            job_type,handler_version,digest,session_id,turn_id):
            raise IntegrityError('Job 去重键对应不同输入')
        return job
    identity = job_id or str(uuid.uuid4())
    now = milliseconds()
    conn.execute('INSERT INTO jobs (id,job_type,handler_version,session_id,turn_id,dedupe_key,'
                 'input_hash,input_json,status,created_at,available_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                 (identity,job_type,handler_version,session_id,turn_id,dedupe_key,digest,payload,
                  'pending',now,now if available_at is None else available_at))
    return read_job(conn,identity)

def require_lease(conn, job, now):
    current = read_job(conn,job.id)
    if (current is None or current.status != 'running' or not job.lease_token
        or current.lease_token != job.lease_token or current.lease_owner != job.lease_owner
        or current.lease_expires_at is None or current.lease_expires_at <= now):
        raise LeaseLostError('Job 领取凭证已过期或被替换')
    return current

def complete_job(conn, job, result, *, now):
    current = read_job(conn,job.id)
    if current is not None and current.status == 'completed':
        if current.lease_token == job.lease_token and current.result == result:
            return current
        raise LeaseLostError('Job 已由其他提交完成')
    require_lease(conn,job,now)
    conn.execute("UPDATE jobs SET status='completed',result_json=?,completed_at=?,"
                 "lease_expires_at=NULL,revision=revision+1 WHERE id=?",
                 (dumps(result),now,job.id))
    return read_job(conn,job.id)

class SqliteJobStore:
    def __init__(self, database, *, clock=milliseconds):
        self.db = database
        self.clock = clock

    def enqueue(self, **kwargs):
        with self.db.write() as conn:
            return enqueue_job(conn,**kwargs)

    def get(self, job_id):
        with self.db.read() as conn:
            return read_job(conn,job_id)

    def list(self, *, job_type=None, status=None, session_id=None, limit=100, offset=0):
        clauses, args = [], []
        for column, value in [('job_type',job_type),('status',status),('session_id',session_id)]:
            if value is not None:
                clauses.append(column+'=?')
                args.append(value)
        where = ' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.db.read() as conn:
            return tuple(_decode(r) for r in conn.execute('SELECT '+_FIELDS+' FROM jobs'+where+
                ' ORDER BY created_at,id LIMIT ? OFFSET ?',(*args,min(100,max(1,limit)),max(0,offset))))

    def claim(self, *, job_type, worker_id, lease_ms=300000, now=None, reclaim_expired=True,
              handler_version=None):
        now = self.clock() if now is None else now
        if lease_ms <= 0:
            raise ValueError('lease_ms 必须为正数')
        with self.db.write() as conn:
            version_filter = ' AND handler_version=?' if handler_version else ''
            args = [job_type]
            if handler_version:
                args.append(handler_version)
            condition = "(status IN ('pending','retryable_failed') AND available_at<=?)"
            args.append(now)
            if reclaim_expired:
                condition += " OR (status='running' AND lease_expires_at<=?)"
                args.append(now)
            row = conn.execute('SELECT id FROM jobs WHERE job_type=?'+version_filter+' AND ('+condition+
                               ') ORDER BY available_at,id LIMIT 1',args).fetchone()
            if not row:
                return None
            conn.execute("UPDATE jobs SET status='running',lease_owner=?,lease_token=?,lease_expires_at=?,"
                         "attempt_count=attempt_count+1,revision=revision+1,started_at=COALESCE(started_at,?) WHERE id=?",
                         (worker_id,str(uuid.uuid4()),now+lease_ms,now,row[0]))
            return read_job(conn,row[0])

    def renew(self, job, *, lease_ms=300000, now=None):
        if lease_ms <= 0:
            raise ValueError("lease_ms must be positive")
        now = self.clock() if now is None else now
        with self.db.write() as conn:
            require_lease(conn,job,now)
            conn.execute('UPDATE jobs SET lease_expires_at=?,revision=revision+1 WHERE id=?',
                         (now+lease_ms,job.id))
            return read_job(conn,job.id)

    def complete(self, job, result, *, now=None):
        with self.db.write() as conn:
            return complete_job(conn,job,result,now=self.clock() if now is None else now)

    def fail(self, job, error, *, available_at, max_attempts=3, now=None):
        now = self.clock() if now is None else now
        with self.db.write() as conn:
            current = require_lease(conn,job,now)
            status = 'terminal_failed' if current.attempt_count >= max_attempts else 'retryable_failed'
            conn.execute('UPDATE jobs SET status=?,last_error=?,available_at=?,lease_token=NULL,'
                         'lease_owner=NULL,lease_expires_at=NULL,revision=revision+1 WHERE id=?',
                         (status,error,available_at,job.id))
            return read_job(conn,job.id)

    def retry(self, job_id, *, expected_revision, available_at=None):
        with self.db.write() as conn:
            job = read_job(conn,job_id)
            if job is None or job.revision != expected_revision or job.status not in (
                'retryable_failed','terminal_failed','cancelled'):
                raise RevisionConflictError('只有当前版本的失败或取消作业可重试')
            conn.execute("UPDATE jobs SET status='pending',available_at=?,last_error=NULL,lease_token=NULL,"
                         "lease_expires_at=NULL,lease_owner=NULL,revision=revision+1 WHERE id=?",
                         (self.clock() if available_at is None else available_at,job_id))
            return read_job(conn,job_id)

    def cancel(self, job_id, *, expected_revision, reason):
        with self.db.write() as conn:
            job = read_job(conn,job_id)
            if job is None or job.revision != expected_revision or job.status == 'completed':
                raise RevisionConflictError('Job 已更改或已完成')
            conn.execute("UPDATE jobs SET status='cancelled',last_error=?,lease_token=NULL,"
                         "lease_expires_at=NULL,lease_owner=NULL,revision=revision+1 WHERE id=?",(reason,job_id))
            return read_job(conn,job_id)
