# Third-party notices

SchedNav does not vendor dataset archives or the AgentTeams source tree.

The root `LICENSE` applies to first-party SchedNav code and documentation. It does not relicense upstream license texts, patches, datasets or any other third-party material identified below.

- Alibaba `cluster-trace-v2026-spot-gpu` and `cluster-trace-gpu-v2023` are pinned through clusterdata commit `0d0f3f1efdbf1add6a7bcc63676eafbd1eb11f71`. No license file was found in the inspected pinned repository tree, so their raw Trace and per-job derivatives are not redistributed.
- Microsoft Philly GPU Trace is pinned to `msr-fiddle/philly-traces` commit `29a1b87fa2d9ed80b83c9e3a37f3a88d382b031d` and published under CC BY 4.0. SchedNav includes only a first-party schema adapter and does not redistribute the source archive or converted per-job data.
- AgentTeams is pinned to version `v1.2.1`, commit `552d0fb54d697b0689dafb6a01740e1a5f507552`, and licensed Apache-2.0. `patches/agentteams/windows-appservice-parity.patch` remains subject to the upstream Apache-2.0 terms, retained at `third_party/licenses/AgentTeams-Apache-2.0.txt`.
- The optional `forecast` extra installs NumPy (`>=1.26,<3`, BSD-3-Clause), PyTorch (`>=2.4,<3`, BSD-3-Clause) and `chinese-calendar` (`>=1.11,<2`, MIT). These packages are resolved by the user's Python package installer and are not vendored or redistributed in this repository.

The machine-readable source, version, integration and redistribution declarations are in `third_party/manifest.json`.
