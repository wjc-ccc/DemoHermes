"""新建库迁移；旧数据库必须显式重建，不自动转换或删除。"""
import hashlib
from pathlib import Path
from .codec import milliseconds

_SCHEMA_DIR = Path(__file__).with_name('schema')

def migrate(conn, *, extra_schema_dirs=()):
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if existing - {'schema_migrations', 'sqlite_sequence'}:
        if 'schema_migrations' not in existing or not conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version='001_runtime'"
        ).fetchone():
            raise RuntimeError('旧数据库不受支持；请停服后显式重建为 agent.db')
    conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations ('
                 'version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at INTEGER NOT NULL)')
    conn.commit()
    # ACTION-HOOK-05：RuntimeStorage 传入可选模块迁移目录；关闭时仅加载原 schema。
    paths = list(_SCHEMA_DIR.glob('*.sql'))
    for directory in extra_schema_dirs:
        paths.extend(Path(directory).glob('*.sql'))
    if len({path.stem for path in paths}) != len(paths):
        raise ValueError('migration 编号/名称冲突')
    for path in sorted(paths, key=lambda path: path.stem):
        sql = path.read_text(encoding='utf-8')
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        row = conn.execute('SELECT checksum FROM schema_migrations WHERE version=?', (path.stem,)).fetchone()
        if row:
            if row[0] != checksum:
                raise RuntimeError(f'已应用 migration 被修改: {path.name}')
            continue
        # DDL 和版本记录在同一事务提交。
        try:
            conn.executescript('BEGIN IMMEDIATE;\n' + sql)
            conn.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                         (path.stem, checksum, milliseconds()))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
