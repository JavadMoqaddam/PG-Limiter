"""Read-only aggregate/node IP comparison without application imports or raw IPs."""

import argparse
import asyncio
from collections import Counter
import json
import os
import time
from urllib.parse import urlsplit

import httpx

MAX_NODES = 128
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TIMESTAMP_EPOCH = 253_402_300_799  # Last second representable by datetime (year 9999).


def _node_summary(value, node_id, received_wall):
    result = {"node_id": node_id, "state": "malformed"}
    if value is None:
        result["state"] = "null"
    elif isinstance(value, dict) and isinstance(value.get("ips"), dict):
        values = value["ips"].values()
        epochs = [stamp for stamp in values if type(stamp) is int and 1_000_000_000 <= stamp <= MAX_TIMESTAMP_EPOCH]
        result.update(
            state="populated" if value["ips"] else "empty",
            ip_count=len(value["ips"]),
            timestamp_types=dict(Counter(type(stamp).__name__ for stamp in values)),
            age_seconds={"min": received_wall - max(epochs), "max": received_wall - min(epochs)} if epochs else None,
            unknown_age_count=len(value["ips"]) - len(epochs),
        )
    return result


def summarize_response(body, *, received_wall, expected_node_ids, node_id=None):
    """Describe source shape and age; a missing/null map remains unknown evidence."""
    if node_id is not None:
        return _node_summary(body, node_id, received_wall)
    if not isinstance(body, dict) or not isinstance(body.get("nodes"), dict):
        return {"state": "malformed"}
    nodes = {}
    for key, value in body["nodes"].items():
        if type(key) is int or (isinstance(key, str) and key.isascii() and key.isdecimal()):
            normalized = int(key)
            if normalized in nodes:
                return {"state": "malformed"}
            nodes[normalized] = value
    present = sorted(nodes)
    missing = sorted(expected_node_ids - nodes.keys())
    return {
        "state": "aggregate",
        "missing_node_ids": missing[:MAX_NODES],
        "missing_nodes_truncated": max(0, len(missing) - MAX_NODES),
        "nodes": [_node_summary(nodes[key], key, received_wall) for key in present[:MAX_NODES]],
        "nodes_truncated": max(0, len(present) - MAX_NODES),
    }


async def collect_samples(client, base, api_key, user_id, node_ids):
    """Compare one aggregate response and the selected nodes, retaining safe summaries."""
    expected = set(node_ids)
    samples = []
    for node_id in [None, *sorted(expected)]:
        endpoint = (
            f"/api/node/online_stats/{user_id}/ip"
            if node_id is None
            else f"/api/node/{node_id}/online_stats/{user_id}/ip"
        )
        sample = {
            "scope": "aggregate" if node_id is None else "node",
            "node_id": node_id,
            "started_wall": time.time(),
            "started_monotonic": time.monotonic(),
        }
        try:
            async with client.stream("GET", base + endpoint, headers={"X-Api-Key": api_key}) as response:
                sample["http_status"] = response.status_code
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError("response_too_large")
                    content.extend(chunk)
                sample.update(received_wall=time.time(), received_monotonic=time.monotonic())
                try:
                    body = json.loads(content)
                except (ValueError, UnicodeError):
                    sample["state"] = "invalid_json"
                else:
                    sample.update(
                        summarize_response(
                            body, received_wall=sample["received_wall"], expected_node_ids=expected, node_id=node_id
                        )
                    )
        except (httpx.HTTPError, ValueError) as error:
            sample.update(state="request_failed", error_type=type(error).__name__)
        samples.append(sample)
        if sample.get("http_status") in (401, 403):
            break
    return samples


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, type=int)
    parser.add_argument(
        "--node-id", required=True, type=int, action="append", help="Expected node; repeat for each node."
    )
    args = parser.parse_args()
    if args.user_id < 1 or any(node < 0 for node in args.node_id) or len(set(args.node_id)) > MAX_NODES:
        parser.error("Use a positive user ID and at most 128 nonnegative node IDs.")
    base = os.environ.get("PANEL_DOMAIN", "").rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        base = "https://" + base
    parts = urlsplit(base)
    key = os.environ.get("PANEL_API_KEY", "")
    if not key or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        parser.error("Export PANEL_DOMAIN and PANEL_API_KEY with a valid panel URL.")
    verify = os.environ.get("PANEL_VERIFY_SSL", "false").lower() in ("true", "1", "yes")
    async with httpx.AsyncClient(timeout=10, verify=verify) as client:
        samples = await collect_samples(client, base, key, args.user_id, args.node_id)
    print(json.dumps({"samples": samples, "node_404_meaning": "ambiguous_upstream_result"}, sort_keys=True))
    return (
        0
        if all(
            sample.get("http_status") == 200 and sample.get("state") in {"aggregate", "empty", "populated"}
            for sample in samples
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
