# Distribution-readiness record

This is the auditable v1 documentation and validation checklist for Issue #6.
It describes repository evidence, not a tag, release, publication, or ruleset
change.

## Bilingual coverage

[`guide.en.md`](guide.en.md) and [`guide.zh-CN.md`](guide.zh-CN.md) use the same
stable coverage markers. `DistributionReadinessTests` requires every marker once
in each guide and checks the corresponding safety vocabulary.

| Coverage ID | English outcome | 中文结果 |
| --- | --- | --- |
| `purpose` | Purpose and shared lifecycle | 用途与共用生命周期 |
| `prerequisites` | Runtime, filesystem, clone, and path prerequisites | 运行时、文件系统、仓库和路径前置条件 |
| `installation` | Safe repository-scoped install and Codex discovery | 安全的仓库范围安装与 Codex 发现 |
| `concepts` | Canonical records, directory roles, and sensitivity | 规范记录、目录角色与敏感度 |
| `safety` | Dry-run, exact approval, no-overwrite/no-delete defaults | 试运行、精确批准、默认不覆盖不删除 |
| `initialize` | Empty-workspace plan through verification | 空工作区从计划到验证 |
| `adoption` | Explicit adopt-in-place without migration | 明确原地接纳且不迁移 |
| `task-updates` | Stable record/path and full validation | 稳定记录/路径与完整验证 |
| `views` | Atomic deterministic local projections | 原子、确定性的本地投影 |
| `archive` | Eligibility, sole destination, and verified move | 资格、唯一目标与验证移动 |
| `rollback` | Evidence-preserving failure and recovery expectations | 保留证据的失败与恢复预期 |
| `limits` | Local v1 limits and read-only dashboard boundary | 本地 v1 限制与只读看板边界 |
| `validation` | Reproducible public repository gates | 可复现的公开仓库门禁 |

## Gate registry

Run the canonical self-contained gate from a clean checkout with Python 3.9 or
later:

```sh
python3 scripts/run_release_gate.py
```

The gate executes these named stages and stops on the first failure:

| Stage | Evidence |
| --- | --- |
| Workspace model validator | Golden workspace conforms to v1 contracts |
| Focused package/model/tooling tests | Skill structure, model, and safe operations |
| Scenario matrix tests | Seven families plus initialization/adoption edge cases |
| Distribution-readiness tests | Bilingual parity, install, links, and public hygiene |
| Full repository tests | No suite is omitted from the delivery candidate |
| Public-content hygiene | Docs/examples/contracts/schemas/scripts/skill/fixtures have safe size, links, and content |
| Isolated forward test | Installed skill runs initialization, adoption, views, and archive |

The repository gate always covers the checked-in skill shape through
`test_skill_package`. When an official `skill-creator` checkout/package is
available, supply its root portably with `--skill-creator-root` or
`SKILL_CREATOR_ROOT`; the gate then runs its `scripts/quick_validate.py` before
all repository stages. The repository contains no machine-specific validator
path.

## Isolated forward-test boundary

`scripts/forward_test_distribution.py` creates a disposable consumer repository,
isolated `HOME`, `CODEX_HOME`, and temporary directory, plus a minimal child
environment built from an explicit allowlist. It does not inherit credential,
profile, virtual-environment, Python-path, SSH-agent, or user-state variables; an
actual child-process sentinel probe verifies that boundary. It first requests
an installation proposal, explicitly confirms the descriptor-anchored staged
copy and atomic no-replace publish, discovers the skill only under the consumer
repository's `.agents/skills`, and invokes the installed dependency-free CLI.

The test then exercises two real workspaces:

1. New workspace: plan initialization, dry-run, exact approval, apply, verify,
   create a valid task record, and generate a TODO view.
2. Existing workspace: register a Unicode/space-containing legacy task and
   material root without moving them, generate a material view, then plan,
   approve, apply, and verify the sole archive move while preserving unrelated
   content.

All artifacts are synthetic and temporary. The test does not read a user skill
directory, developer home state, credentials, network service, dashboard, or
untracked fixture.

## Release boundary

Passing this record means the v1 source tree is ready for review. It does not
mean a tag or release was created, a marketplace/plugin was published, or GitHub
rules were changed. V1 remains usable as a standalone local skill with no HTML
dashboard. Issue #7 may add a read-only static TODO/timeline consumer of the same
canonical generated catalogs; it must keep sensitivity filtering and cannot
become a second data source.
