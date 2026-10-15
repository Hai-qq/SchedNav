"""Synthetic contract fixture: two jobs, one node, no external data or LLM."""
from pathlib import Path
import tempfile

from schednav.native_simulator import SimulationPolicy, simulate_trace
from schednav.native_trace import TraceJob, TraceNode, load_canonical_trace, write_canonical_trace


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="schednav-demo-") as temporary:
        trace = load_canonical_trace(write_canonical_trace(
            Path(temporary),
            trace_id="synthetic-quickstart",
            time_origin="2026-01-01 00:00:00",
            source={"dataset": "synthetic contract fixture", "purpose": "mechanism demo only"},
            nodes=[TraceNode("node-1", "demo-gpu", 2)],
            jobs=[
                TraceJob("spot-1", 0, 10, 2, "Spot", "demo-gpu"),
                TraceJob("hp-1", 2, 2, 2, "HP", "demo-gpu"),
            ],
        ))
        print("Synthetic fixture only; no real-cluster performance claim.\n")
        for scheduler in ("fifo", "priority_preemptive"):
            policy = SimulationPolicy.from_dict({
                "schema_version": "schednav.simulation-policy/v1",
                "action_id": f"demo-{scheduler}",
                "scheduler": scheduler,
                "spot_guarantee_seconds": 0,
                "checkpoint_interval_seconds": 2,
                "preemption_overhead_seconds": 0,
                "placement_strategy": "deterministic_best_fit",
            })
            result = simulate_trace(trace, policy)
            hp = next(job for job in result["jobs"] if job["job_id"] == "hp-1")
            print(f"{scheduler}: HP queue = {hp['queue_seconds']:g}s; "
                  f"preemptions = {len(result['preemption_events'])}")
            print(f"  result fingerprint: {result['result_fingerprint']}")


if __name__ == "__main__":
    main()
