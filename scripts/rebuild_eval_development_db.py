"""Stage and verify a new development database; replace only with --replace.

The old database is always backed up using SQLite backup, including WAL content.
Run after stopping the development server. No historical migration is rewritten.
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent.eval.datasets.memory_dataset import load_defaults
from agent.eval.datasets.long_dialogues import load_long_datasets
from agent.storage.sqlite.runtime_storage import RuntimeStorage


def stage(path, evaluation_dirs=()):
    target=Path(path).resolve()
    if target.parent!=ROOT/"data" or target.name!="agent.db":
        raise ValueError("this development rebuild is restricted to workspace data/agent.db")
    stamp=time.strftime("%Y%m%d-%H%M%S",time.gmtime())
    staged=target.with_name(f"agent.staged.{stamp}.db")
    if staged.exists():
        raise FileExistsError(staged)
    storage=RuntimeStorage.open(staged)
    for definition in (*load_defaults(),*load_long_datasets()):
        storage.evals.publish(definition)
    imported_runs=import_results(storage,evaluation_dirs)
    with storage.db.read() as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0]!="ok" or conn.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("staged database integrity check failed")
        cases=conn.execute("SELECT COUNT(*) FROM eval_cases").fetchone()[0]
        questions=conn.execute("SELECT COUNT(*) FROM eval_questions").fetchone()[0]
        if (cases,questions)!=(91,140):
            raise ValueError("staged dataset counts mismatch")
    with closing(sqlite3.connect(staged)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    return dict(target=str(target),staged=str(staged),cases=cases,questions=questions,
                imported_runs=imported_runs,timestamp_zone="UTC")


def import_results(storage,evaluation_dirs):
    imported=[]
    for directory in evaluation_dirs:
        source_path=Path(directory).resolve()/"evaluations.db"
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        with closing(sqlite3.connect(f"{source_path.as_uri()}?mode=ro",uri=True)) as source:
            if source.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
                raise ValueError("evaluation source integrity check failed")
            with storage.db.write() as destination:
                for table in ("eval_runs","eval_results"):
                    cursor=source.execute(f"SELECT * FROM {table}")
                    names=[column[0] for column in cursor.description]
                    rows=cursor.fetchall()
                    if table=="eval_runs" and any(row[names.index("finished_at")] is None for row in rows):
                        raise ValueError("evaluation source has unfinished runs")
                    columns=",".join(names)
                    placeholders=",".join("?" for _ in names)
                    destination.executemany(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",rows)
                    if table=="eval_runs":
                        imported.extend(row[names.index("run_id")] for row in rows)
    return imported


def replace(staged_report):
    target=Path(staged_report["target"]).resolve()
    staged=Path(staged_report["staged"]).resolve()
    if target.parent!=ROOT/"data" or staged.parent!=target.parent:
        raise ValueError("replacement paths must stay within workspace data")
    backup=target.with_name("agent.pre_long_repair."+staged.stem.split(".")[-1]+".db")
    if target.is_file():
        if backup.exists():
            raise FileExistsError(backup)
        # An exclusive lock/checkpoint makes busy development writers fail before replacement.
        with closing(sqlite3.connect(target,timeout=2)) as source:
            source.execute("PRAGMA busy_timeout=2000")
            row=source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row and row[0]:
                raise RuntimeError("development database is busy; stop its server before replacing")
            with closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
                    raise ValueError("backup integrity check failed")
            source.execute("PRAGMA journal_mode=DELETE")
            source.execute("BEGIN EXCLUSIVE")
            source.rollback()
    # Exact checked paths; the backup is complete before the reversible replacement.
    staged.replace(target)
    report={**staged_report,"backup":str(backup),"replaced":True}
    target.with_name("long-repair-rebuild-report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",type=Path,default=ROOT/"data/agent.db")
    parser.add_argument("--replace",action="store_true")
    parser.add_argument("--evaluation-dir",action="append",type=Path,default=[])
    args=parser.parse_args()
    report=stage(args.db,args.evaluation_dir)
    if args.replace:
        report=replace(report)
    print(json.dumps(report,ensure_ascii=False))


if __name__=="__main__":
    main()
