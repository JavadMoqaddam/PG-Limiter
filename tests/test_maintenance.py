"""Retention has an owner: one pass prunes both the violation history and the ISP
subnet cache that otherwise grew unbounded with no live caller."""

import contextlib

import utils.maintenance as maintenance


async def test_run_retention_once_prunes_both_stores(monkeypatch):
    calls = []

    class FakeViolations:
        @staticmethod
        async def cleanup_old(db, days):
            calls.append(("violations", days))
            return 3

    class FakeSubnet:
        @staticmethod
        async def cleanup_old(db, days):
            calls.append(("subnet", days))
            return 5

    @contextlib.asynccontextmanager
    async def fake_get_db():
        yield object()

    monkeypatch.setattr(maintenance, "DB_AVAILABLE", True)
    monkeypatch.setattr(maintenance, "get_db", fake_get_db)
    monkeypatch.setattr(maintenance, "ViolationHistoryCRUD", FakeViolations)
    monkeypatch.setattr(maintenance, "SubnetISPCRUD", FakeSubnet)

    result = await maintenance.run_retention_once(days=30)

    assert result == {"violations": 3, "subnet_isp": 5}
    assert ("violations", 30) in calls
    assert ("subnet", 30) in calls


async def test_run_retention_once_noop_without_db(monkeypatch):
    monkeypatch.setattr(maintenance, "DB_AVAILABLE", False)
    result = await maintenance.run_retention_once(days=30)
    assert result == {"violations": 0, "subnet_isp": 0}
