# workspace-organizer-skill

Clean-slate hybrid workspace management for Obsidian and Chat/Agent. A Task is
an independently deliverable outcome; Captures and file Artifacts remain separate
until a deliberate triage decision. Canonical state is human-readable Markdown,
generated pages are disposable projections, and archive/restore operations are
audited and recoverable.

The private cockpit includes all sensitivity levels. Agent access is a separate
`none/metadata/content` policy, and filtered output is produced only through an
explicit export profile.

中文简介：这是面向 Obsidian 与 Chat/Agent 的 clean-slate 混合式工作区管理技能。
Task 表示一个可交付结果，Capture 与文件 Artifact 在明确分拣前保持独立；Markdown
是唯一事实来源，视图可以重建，归档和恢复必须经过可审计的精确批准。
个人驾驶舱默认完整；Agent 访问采用独立的 `none/metadata/content` 权限，只有
显式导出才按敏感度过滤。

- [English installation and user guide](docs/guide.en.md)
- [中文安装与使用指南](docs/guide.zh-CN.md)
- [Clean-slate English user guide](docs/guide.clean-slate.en.md)
- [Clean-slate 中文用户指南](docs/guide.clean-slate.zh-CN.md)
- [Clean-slate 混合式设计合同（当前设计基线）](docs/design-contract.zh-CN.md)
- [Normative v1 workspace model](docs/workspace-model.md)
- [Auditable distribution-readiness checklist](docs/distribution-readiness.md)
- [Optional read-only dashboard contract](skill/workspace-organizer/references/dashboard.md)
- [Official OpenAI skill documentation](https://learn.chatgpt.com/docs/build-skills)

Prerequisite: Python 3.9 or later on a supported POSIX filesystem. Run the
complete dependency-free repository gate from a clean checkout:

```sh
python3 scripts/run_release_gate.py
```

This repository does not tag, publish, or release anything as part of the gate.
The clean-slate CLI is dependency-free and works with Obsidian closed. The old
v1 CLI/dashboard files remain only as explicit historical reference; new work
must use `skill/workspace-organizer/scripts/clean_slate.py` and schema v2.
