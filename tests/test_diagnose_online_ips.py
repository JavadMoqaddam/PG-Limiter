"""Online-IP diagnosis must preserve response distinctions without exposing IPs."""

import json

import httpx
import pytest

import tools.diagnose_online_ips as diagnose_online_ips
from tools.diagnose_online_ips import summarize_response


def test_aggregate_summary_distinguishes_missing_null_empty_and_timestamp_types():
    body = {
        "nodes": {
            "1": {"ips": {}},
            "2": None,
            "3": {"ips": {"private-ip": 1_800_000_001}},
            "4": {"ips": {"other-private-ip": "private-value", "third-ip": True}},
        }
    }
    result = summarize_response(body, received_wall=1_800_000_002, expected_node_ids={1, 2, 3, 4, 5})
    assert result["missing_node_ids"] == [5]
    assert [node["state"] for node in result["nodes"]] == ["empty", "null", "populated", "populated"]
    assert result["nodes"][2]["timestamp_types"] == {"int": 1}
    assert result["nodes"][2]["age_seconds"] == {"min": 1, "max": 1}
    assert result["nodes"][3]["timestamp_types"] == {"str": 1, "bool": 1}
    assert result["nodes"][3]["age_seconds"] is None
    assert all(value not in json.dumps(result) for value in ("private-ip", "private-value", "third-ip"))


def test_node_404_and_malformed_maps_do_not_become_empty_evidence():
    assert (
        summarize_response(
            {"detail": "Node unavailable or user not found"}, received_wall=0, expected_node_ids={1}, node_id=1
        )["state"]
        == "malformed"
    )
    assert summarize_response({"ips": {}}, received_wall=0, expected_node_ids={1}, node_id=1)["state"] == "empty"
    result = summarize_response({"nodes": {"1": {"ips": []}}}, received_wall=0, expected_node_ids={1})
    assert result["nodes"][0]["state"] == "malformed"


def test_summary_bounds_nodes_and_ignores_untrusted_node_keys():
    result = summarize_response(
        {"nodes": {**{str(i): {"ips": {}} for i in range(200)}, "private-key": None}},
        received_wall=0,
        expected_node_ids=set(range(200)),
    )
    assert len(result["nodes"]) == 128
    assert result["nodes_truncated"] == 72
    assert "private-key" not in json.dumps(result)


def test_unrepresentable_timestamp_preserves_summary_without_exposing_values():
    result = summarize_response(
        {"ips": {"private-ip": 10**400}}, received_wall=1_800_000_002.0, expected_node_ids={1}, node_id=1
    )
    assert result["state"] == "populated"
    assert result["age_seconds"] is None
    assert result["unknown_age_count"] == 1
    assert "private-ip" not in json.dumps(result)


@pytest.mark.parametrize("content", [b"not-json", b'{"unexpected": true}', b"x" * (2 * 1024 * 1024 + 1)])
async def test_cli_reports_unusable_http_200_as_failure(monkeypatch, capsys, content):
    def respond(request):
        return httpx.Response(200, content=content)

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        diagnose_online_ips.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond))
    )
    monkeypatch.setenv("PANEL_DOMAIN", "https://panel.test")
    monkeypatch.setenv("PANEL_API_KEY", "pg_synthetic_private_key")
    monkeypatch.setattr("sys.argv", ["diagnose_online_ips", "--user-id", "1", "--node-id", "63"])
    assert await diagnose_online_ips.main() == 1
    output = json.loads(capsys.readouterr().out)
    assert output["samples"][0]["http_status"] == 200
    assert "pg_synthetic_private_key" not in json.dumps(output)
