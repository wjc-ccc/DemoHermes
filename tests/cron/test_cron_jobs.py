"""JobStore：按 scope 隔离，三种时间的到点与推进。"""
from datetime import datetime

import pytest

from agent.core.cron.jobs import CronJob, JobStore, next_daily, parse_at
from agent.types.memory.scope import MemoryScope

ALICE = MemoryScope(tenant_id="feishu", user_id="ou_alice")
BOB = MemoryScope(tenant_id="feishu", user_id="ou_bob")
NOW = datetime(2026, 10, 16, 10, 0).timestamp()


def _job(**over) -> CronJob:
    return CronJob(**{"session_key": "agent:main:feishu:dm:oc_1:user:ou_alice", "next_run": NOW, **over})


def test_next_daily_today_or_tomorrow():
    assert next_daily("11:30", NOW) == datetime(2026, 10, 16, 11, 30).timestamp()
    assert next_daily("09:00", NOW) == datetime(2026, 10, 17, 9, 0).timestamp()
    assert next_daily("10:00", NOW) == datetime(2026, 10, 17, 10, 0).timestamp()


def test_parse_at_accepts_time_or_datetime():
    assert parse_at("09:00", NOW) == datetime(2026, 10, 17, 9, 0).timestamp()
    assert parse_at("2026-10-20 08:15", NOW) == datetime(2026, 10, 20, 8, 15).timestamp()
    with pytest.raises(ValueError):
        parse_at("25:00", NOW)


def test_jobs_are_isolated_by_scope(tmp_path):
    store = JobStore(tmp_path)
    store.add(ALICE, _job(text="a"))
    store.add(BOB, _job(text="b"))
    assert [job.text for job in store.list(ALICE)] == ["a"]
    assert [job.text for job in store.list(BOB)] == ["b"]
    assert (tmp_path / ALICE.storage_key / "scope.json").exists()


def test_remove_only_touches_own_scope(tmp_path):
    store = JobStore(tmp_path)
    job = store.add(ALICE, _job(text="a"))
    assert store.remove(BOB, job.job_id) is False
    assert store.remove(ALICE, job.job_id) is True
    assert store.list(ALICE) == []


def test_due_and_advance(tmp_path):
    store = JobStore(tmp_path)
    once = store.add(ALICE, _job(at=NOW, text="once"))
    daily = store.add(ALICE, _job(daily="09:00", text="daily"))
    every = store.add(ALICE, _job(every_minutes=15, text="every"))
    later = store.add(ALICE, _job(at=NOW + 3600, next_run=NOW + 3600, text="later"))
    store.add(BOB, _job(every_minutes=5, next_run=NOW + 60))

    due = store.due(NOW)
    assert {job.job_id for _, job in due} == {once.job_id, daily.job_id, every.job_id}
    for directory, job in due:
        store.advance(directory, job.job_id, NOW)

    remaining = {job.job_id: job for job in store.list(ALICE)}
    assert once.job_id not in remaining
    assert remaining[daily.job_id].next_run == datetime(2026, 10, 17, 9, 0).timestamp()
    assert remaining[every.job_id].next_run == NOW + 15 * 60
    assert remaining[later.job_id].next_run == NOW + 3600
    assert store.due(NOW) == []


def test_corrupt_file_skips_only_that_user(tmp_path):
    store = JobStore(tmp_path)
    store.add(ALICE, _job(text="a"))
    store.add(BOB, _job(text="b"))
    (tmp_path / BOB.storage_key / "jobs.json").write_text("{broken", encoding="utf-8")
    assert [job.text for _, job in store.due(NOW)] == ["a"]
