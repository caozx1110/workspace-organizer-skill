# workspace-organizer-skill

Human- and agent-friendly organization for durable tasks, materials, local
TODO/timeline views, and verified archives. The v1 workflow is deliberately
conservative: inspect first, dry-run every structural plan, approve the exact
plan bytes, then apply and verify. It never overwrites or deletes user content
by default.

中文简介：面向人和智能体的持久工作区整理技能，用规范化任务记录生成本地 TODO、
时间线和材料索引。v1 坚持先检查、再试运行、精确批准计划、执行并验证；默认不覆盖、
不删除用户内容。

- [English installation and user guide](docs/guide.en.md)
- [中文安装与使用指南](docs/guide.zh-CN.md)
- [Normative v1 workspace model](docs/workspace-model.md)
- [Auditable distribution-readiness checklist](docs/distribution-readiness.md)
- [Official OpenAI skill documentation](https://learn.chatgpt.com/docs/build-skills)

Prerequisite: Python 3.9 or later on a supported POSIX filesystem. Run the
complete dependency-free repository gate from a clean checkout:

```sh
python3 scripts/run_release_gate.py
```

This repository does not tag, publish, or release anything as part of the gate.
The v1 skill is fully usable without a dashboard; Issue #7 owns a later
read-only dashboard that consumes the same sensitivity-filtered generated data.
