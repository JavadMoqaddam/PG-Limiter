"""Measure synthetic parser work without panel, Telegram, database or network calls."""

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import telegram_bot.topics as topics  # noqa: E402
import utils.monitoring_gate as gate  # noqa: E402
import utils.parse_logs as parser  # noqa: E402
from utils.log_observations import LOG_OBSERVATIONS, LogObservations  # noqa: E402
from utils.read_config import get_config_revision  # noqa: E402
from utils.shared_state import ACTIVE_USERS, NODE_LAST_EVENT  # noqa: E402

EPOCH = datetime(2026, 10, 23, 10, tzinfo=timezone.utc).timestamp()


def record(index):
    return (
        f"2026/10/23 10:00:20.{index % 1000000:06d} from 192.0.2.1:{10000 + index} "
        "accepted tcp:example.test:443 [Vless >> DIRECT] email: 1.synthetic"
    )


async def measure(records, rounds):
    lines = [record(index) for index in range(records)]
    config = {
        "ip_source": "logs",
        "log_timezone": "UTC",
        "check_interval": 60,
        "log_replay_cache_size": records + 1,
        "_revision": get_config_revision(),
    }
    results = {name: [] for name in ("timestamp", "parser_empty", "parser_live", "parser_replay")}

    async def accepted(*args):
        return True

    with tempfile.TemporaryDirectory(prefix="pg-limiter-parser-bench-") as temp:
        with (
            patch.object(topics, "TOPICS_FILE", str(Path(temp) / "topics.json")),
            patch.object(topics, "MESSAGE_CACHE_FILE", str(Path(temp) / "cache.json")),
        ):
            manager = topics.TopicsManager()
        manager._enabled, manager._group_id = True, -1001234567890
        with (
            patch.object(topics, "_topics_manager", manager),
            patch.object(parser, "read_config", AsyncMock(return_value=config)),
            patch.object(parser, "is_ip_accepted", accepted),
            patch.object(time, "time", lambda: EPOCH + 60),
        ):
            for _ in range(rounds):
                for scenario in results:
                    LOG_OBSERVATIONS.__dict__ = LogObservations().__dict__
                    ACTIVE_USERS.clear()
                    NODE_LAST_EVENT.clear()
                    parser._PARSER_CONFIG_CACHE = None
                    parser._PARSER_CONFIG_REVISION = -1
                    gate._synchronized_admission = None
                    helper = getattr(sys.modules["utils.log_observations"], "_printed_second_timestamp", None)
                    if helper is not None:
                        helper.cache_clear()
                    LOG_OBSERVATIONS.configure(config)
                    LOG_OBSERVATIONS.floor = EPOCH
                    await parser.parse_logs("", 1, "synthetic")
                    started = time.perf_counter()
                    if scenario == "timestamp":
                        for line in lines:
                            LOG_OBSERVATIONS.timestamp(line, 1)
                    else:
                        for line in lines:
                            await parser.parse_logs(
                                "" if scenario == "parser_empty" else lines[0] if scenario == "parser_replay" else line,
                                1,
                                "synthetic",
                            )
                    results[scenario].append(time.perf_counter() - started)
    return {
        "records": records,
        "rounds": rounds,
        "synthetic_replay_capacity": records + 1,
        "median_seconds": {name: round(statistics.median(values), 6) for name, values in results.items()},
    }


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--records", type=int, default=20000)
    arguments.add_argument("--rounds", type=int, default=3)
    args = arguments.parse_args()
    if args.records < 1 or args.rounds < 1:
        arguments.error("records and rounds must be positive")
    print(json.dumps(asyncio.run(measure(args.records, args.rounds)), sort_keys=True))


if __name__ == "__main__":
    main()
