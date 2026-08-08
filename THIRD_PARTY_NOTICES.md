# Third-party notices

SchedNav does not vendor the GFS, Alibaba clusterdata, or AgentTeams source trees.

The root `LICENSE` applies to first-party SchedNav code and documentation. It does not relicense the compatibility patches, upstream license texts, or any other third-party material identified below.

- GFS (`MachineLearningSystem/26ASPLOS-Spot`) is pinned to commit `e998d5453e626a0b743b3fd5137c54c987db780b` and licensed GPL-3.0-only. `patches/gfs/reproduction-gate.patch` is distributed under the upstream GPL-3.0-only terms, with the full license retained at `third_party/licenses/GFS-GPL-3.0.txt`.
- Alibaba `cluster-trace-v2026-spot-gpu` is pinned through clusterdata commit `0d0f3f1efdbf1add6a7bcc63676eafbd1eb11f71`. No license file was found in the inspected pinned repository tree, so the raw Trace and per-job derivatives are not redistributed.
- AgentTeams is pinned to version `v1.2.1`, commit `552d0fb54d697b0689dafb6a01740e1a5f507552`, and licensed Apache-2.0. `patches/agentteams/windows-appservice-parity.patch` remains subject to the upstream Apache-2.0 terms, retained at `third_party/licenses/AgentTeams-Apache-2.0.txt`.

The machine-readable source, version, integration and redistribution declarations are in `third_party/manifest.json`.
