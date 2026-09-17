"""Pure transport contracts for optional external AgentTeams wave runners.

This module performs no network calls and is not a replacement for an operator's
wave executor. It makes the public transport rules independently testable.
"""
from __future__ import annotations

import json
from typing import Any

MCPORTER_OUTPUT_LIMIT = 65_536


def mcp_output_mode(operation: str) -> str:
    return "text" if operation == "read_artifact" else "json"


def transient_mcp_error(operation: str, response: dict[str, Any]) -> bool:
    # Retry only the known artifact-visibility transport failure, never writes.
    return operation == "read_artifact" and response.get("error") == (
        "SSE error: Non-200 status code (404)"
    )


def truncated_artifact_receipt(
    operation: str, arguments: dict[str, Any], output: str
) -> dict[str, Any]:
    if operation != "read_artifact" or len(output) != MCPORTER_OUTPUT_LIMIT:
        raise ValueError("Not an exact-cap artifact read")
    reference = arguments.get("artifact_ref")
    if not isinstance(reference, str) or not reference:
        raise ValueError("Missing artifact_ref")
    try:
        json.loads(output)
    except json.JSONDecodeError:
        pass
    else:
        raise ValueError("Complete JSON must not be treated as truncated")
    return {
        "artifact_ref": reference,
        "read_status": "omitted_mcporter_output_limit",
        "output_characters": len(output),
        "requires_trusted_collector": True,
    }


def artifact_read_satisfied(artifact_kind: str, receipt: dict[str, Any]) -> bool:
    # This exception records an omitted terminal report, not verified contents.
    # Metrics and other decision inputs must never pass on an omission receipt.
    return (
        artifact_kind == "rolling_control_report"
        and receipt.get("read_status") == "omitted_mcporter_output_limit"
        and receipt.get("output_characters") == MCPORTER_OUTPUT_LIMIT
        and receipt.get("requires_trusted_collector") is True
        and isinstance(receipt.get("artifact_ref"), str)
        and bool(receipt["artifact_ref"])
    )
