# Workspace Organizer v1 — 中文指南

本指南描述公开的 v1 软件包。规范行为以[工作区模型](workspace-model.md)为准，
CLI 的详细保证以技能中的[工具参考](../skill/workspace-organizer/references/tooling.md)为准。

<!-- coverage:purpose -->
## 用途

`workspace-organizer` 在一个持久的本地工作区中维护规范 `TASK.md`、可复用材料、
生成的 TODO/时间线/材料视图和经过验证的归档。不同任务类型共用元数据与同一生命周期，
不为每类任务另造工作流。

所有结构性变更都遵循以下安全顺序：

```text
scan -> proposal -> dry-run -> exact approval -> apply -> verify
```

扫描或提案不代表写入许可。必须先试运行，再由 `approve --yes` 记录用户对精确计划
字节的精确批准；一旦修改计划，批准立即失效。默认不覆盖、不删除、不发布，也不推断
材料归属。

<!-- coverage:prerequisites -->
## 前置条件

- Python 3.9 或更高版本；软件包不依赖第三方 Python 库。
- macOS 或 Linux，并且文件系统支持安全执行管线使用的原子不替换/交换操作；
  不支持的 POSIX 文件系统会安全失败。
- 安装和仓库验证需要本仓库的本地克隆。
- 按仓库范围安装时，需要一个已经存在的目标仓库根目录。
- 用户自行选择工作区和私有计划目录的绝对路径。计划、批准和证据可能包含元数据与
  哈希，应只保存在本地。

下面的命令使用这些 shell 变量。运行操作示例前，将它们设置为真实存在的位置：

```sh
export WO_DISTRIBUTION_ROOT="$PWD"
export WO_CONSUMER_REPO="/absolute/path/to/consumer-repository"
export WO_ROOT="/absolute/path/to/workspace"
export WO_PLAN_ROOT="/absolute/path/to/private-plan-directory"
export WO_TOOL="$WO_CONSUMER_REPO/.agents/skills/workspace-organizer/scripts/workspace_organizer.py"
```

<!-- coverage:installation -->
## 安装并发现技能

在分发仓库根目录先检查精确的“不替换”目标，再确认复制：

```sh
python3 scripts/install_skill.py --target-root "$WO_CONSUMER_REPO"
python3 scripts/install_skill.py --target-root "$WO_CONSUMER_REPO" --yes
```

安装器创建 `$WO_CONSUMER_REPO/.agents/skills/workspace-organizer`；如果目标已经存在
或包中含符号链接，就拒绝继续。它不会更新、替换或移除已安装技能。
来源与目标遍历均由目录描述符锚定且不跟随链接。安装器先在 `.agents` 下创建随机的
`0700` staging 目录，使其位于扫描目录 `.agents/skills` 之外；完整且已验证的副本再
通过一次跨目录原子“不替换”重命名发布，竞态或部分失败不会暴露最终目标。安装器绝不
递归删除 staging；失败证据会在 `.agents/.workspace-organizer.install-<random>` 这个
非扫描位置隔离保留。人工核对后才能移除该目录；重试会使用新的随机名称。发布后若身份
或父目录检查失败，当前规范条目会通过原子重命名移回该非扫描隔离位置，不会被删除；
无法安全移回时会明确报告发布状态未知。Codex 会按
[OpenAI 官方技能文档](https://learn.chatgpt.com/docs/build-skills#where-to-save-skills)
扫描仓库中的 `.agents/skills`。新技能未出现时才需要重启 Codex。

确认安装通常返回 `installed`。如果原子重命名已经提交、但无法确认父目录的持久性，
安装器会重新验证规范目标，并返回 `installed-with-durability-warning`；该结果仍表示安装
成功，但应在系统重启后再次检查。

这是本地/仓库范围的安装，不是发布或发行。官方文档建议用插件做更广泛的可复用分发；
插件打包不属于 v1，也不属于本仓库门禁。

<!-- coverage:concepts -->
## 工作区概念

规范输入是 `.workspace-organizer/config.json` 和每个已登记任务根目录中的唯一
`TASK.md`。`00_总览/` 与 catalog JSON 都是派生视图，绝不是第二数据源。

| 路径 | 角色 |
| --- | --- |
| `00_总览/` | 生成的 TODO、时间线和材料投影 |
| `10_收件箱/` | 未分类、默认按 `restricted` 处理的来件 |
| `20_任务/` | 路径稳定的活动任务包 |
| `30_资料库/` | 不从属于单一任务的复用材料 |
| `90_归档/` | 已验证的终态任务包 |
| `99_待整理/` | 等待明确决策的内容 |
| `.workspace-organizer/` | 本地配置、计划、目录和证据 |

任务 ID 是稳定的小写 ASCII slug。元数据更新不移动任务；唯一正常的任务包移动是经过
验证的归档。默认视图只纳入 `public` 和 `internal`，并在渲染、计数、排序或哈希前
过滤 `confidential`、`restricted`、未知或格式错误的敏感度。

<!-- coverage:safety -->
## 安全操作模型

1. 运行只读的 `inventory` 或 `scan`。
2. 创建不可变的 `plan-init`、`plan-organize` 或 `plan-archive` 文件。
3. 运行 `dry-run`，检查全部预期变更、冲突和跳过的边界。
4. 只有用户接受精确计划后，才用 `approve --yes` 创建独立批准文件。
5. 运行 `apply`，再要求 `verify` 返回已验证证据。

不能把批准用于已修改的计划，也不能用临时 shell 移动绕过 CLI。来源过期、目标冲突、
符号链接、嵌套 Git 边界、无生成标记的视图或未完成的旧操作都会让流程停止。

<!-- coverage:initialize -->
## 初始化新工作区

先自行创建空工作区和私有计划目录。初始化计划必须放在尚未初始化的工作区之外。然后运行：

```sh
python3 "$WO_TOOL" inventory "$WO_ROOT"
python3 "$WO_TOOL" plan-init "$WO_ROOT" --workspace-id example-workspace --output "$WO_PLAN_ROOT/init.json"
python3 "$WO_TOOL" dry-run "$WO_ROOT" --plan "$WO_PLAN_ROOT/init.json"
python3 "$WO_TOOL" approve --plan "$WO_PLAN_ROOT/init.json" --output "$WO_PLAN_ROOT/init.approval.json" --yes
python3 "$WO_TOOL" apply "$WO_ROOT" --plan "$WO_PLAN_ROOT/init.json" --approval "$WO_PLAN_ROOT/init.approval.json"
python3 "$WO_TOOL" verify "$WO_ROOT" --plan "$WO_PLAN_ROOT/init.json"
```

工作区 ID 应为稳定的 3–64 位小写字符串。除非用户明确选择其他值，新记录默认敏感度为
`internal`。

<!-- coverage:adoption -->
## 原地接纳现有工作区

先做清单。每个旧任务根和材料根都必须由用户选择；接纳不会推断归属、重命名、移动、
规范化或删除现有内容。已有的受管目录必须逐项明确接受。例如：

```sh
python3 "$WO_TOOL" inventory "$WO_ROOT"
python3 "$WO_TOOL" plan-init "$WO_ROOT" --workspace-id adopted-workspace --adopt-task "Existing Projects/研究 α" --adopt-material "Legacy Library/资料 with spaces=internal" --accept-existing-managed "10_收件箱" --output "$WO_PLAN_ROOT/adopt.json"
python3 "$WO_TOOL" dry-run "$WO_ROOT" --plan "$WO_PLAN_ROOT/adopt.json"
python3 "$WO_TOOL" approve --plan "$WO_PLAN_ROOT/adopt.json" --output "$WO_PLAN_ROOT/adopt.approval.json" --yes
python3 "$WO_TOOL" apply "$WO_ROOT" --plan "$WO_PLAN_ROOT/adopt.json" --approval "$WO_PLAN_ROOT/adopt.approval.json"
python3 "$WO_TOOL" verify "$WO_ROOT" --plan "$WO_PLAN_ROOT/adopt.json"
```

制定计划前，每个选中的任务根都必须已有一份有效 `TASK.md`。未选择或不确定的内容
保持未受管并留在原地。

<!-- coverage:task-updates -->
## 创建或更新任务

从已安装包的 `assets/TASK.md` 开始，替换所有示例事实，并在已登记任务包根目录放置
唯一记录。保留稳定 ID 与路径、遵守共用生命周期、每次语义编辑都推进 `updated`，
并在编辑前后验证整个工作区：

```sh
python3 scripts/validate_workspace_model.py "$WO_ROOT"
```

v1 没有任务编辑命令。记录编辑是用户/智能体有意进行的文本编辑，不代表可以重组文件。
无效或重复记录会阻止视图生成，工具不会猜测如何修复。

<!-- coverage:views -->
## 生成本地视图

把三份 JSON catalog 和三份 Markdown 投影作为一个原子集合重新生成：

```sh
python3 "$WO_TOOL" index "$WO_ROOT"
```

输入未变时，输出逐字节相同。命令只替换带有同类 v1 生成标记的文件；无标记的总览
属于用户，会阻止生成。视图从不复制任务正文或文件内容；敏感元数据会在哈希或计数前过滤。

<!-- coverage:archive -->
## 归档终态任务

只有有效的 `completed` 或 `cancelled` 任务，且任务包没有 pending/未分配内容时才可
归档。唯一目标是 `90_归档/<closed-year>/<area>/<task-id>/`。检查并批准精确计划：

```sh
python3 "$WO_TOOL" plan-archive "$WO_ROOT" --task-id example-task --archived-at "2026-08-19T14:00:00+08:00" --output "$WO_PLAN_ROOT/archive.json"
python3 "$WO_TOOL" dry-run "$WO_ROOT" --plan "$WO_PLAN_ROOT/archive.json"
python3 "$WO_TOOL" approve --plan "$WO_PLAN_ROOT/archive.json" --output "$WO_PLAN_ROOT/archive.approval.json" --yes
python3 "$WO_TOOL" apply "$WO_ROOT" --plan "$WO_PLAN_ROOT/archive.json" --approval "$WO_PLAN_ROOT/archive.approval.json"
python3 "$WO_TOOL" verify "$WO_ROOT" --plan "$WO_PLAN_ROOT/archive.json"
```

验证通过的归档会更新规范记录，并且只移动整个任务包一次；绝不覆盖已有目标。

<!-- coverage:rollback -->
## 回滚与失败预期

v1 记录不可变意图、预写阶段、前后哈希、旧任务字节和配置变更。受控写入失败时，索引
生成会自动恢复之前的六文件集合。结构操作只有在成功验证后才可幂等重放。

系统刻意不提供通用自动回滚命令。发生中断、验证失败或意图未完成时应立即停止；保留
工作区、计划、批准、WAL、验证证据以及部分复制的两端。不得把删除来源或覆盖目标当作
恢复手段。只能依据保留的精确证据做有边界的人工恢复，并在旧状态核对完成后再准备新计划。
错误归档只能作为该次精确归档操作的已验证回滚来撤销。

<!-- coverage:limits -->
## 已知限制

- v1 是本地文件系统工具；不负责发布、同步、调度、去重、OCR、内容嵌入、数据库或 Web 服务。
- 隐藏控制目录只用于组织，不是访问控制边界；文件系统权限仍由用户负责。
- 清单不会跟随符号链接或进入嵌套 Git 仓库。压缩原件默认只读元数据；只有另行确认的
  有界列表才会打开，工具永不解压。
- 扫描大文件时可以延迟哈希；生成材料 catalog 时仍只对符合可见条件的材料做内容哈希。
- v1 没有 HTML 看板，初始化、接纳、更新任务、生成视图和归档都不需要看板。Issue #7
  负责后续只读静态消费者，它读取同一份规范且按敏感度过滤的数据，不能变成可编辑或权威数据源。

<!-- coverage:validation -->
## 验证分发就绪状态

在干净仓库中使用 Python 3.9 或更高版本运行以下命令；它们自包含且不发起网络请求：

```sh
python3 scripts/check_public_content.py
python3 scripts/forward_test_distribution.py
python3 scripts/run_release_gate.py
```

统一门禁会运行模型验证器、聚焦的包/模型/工具测试、场景矩阵、分发测试、完整测试套件、
公开内容与链接卫生检查，以及已安装包的隔离前向测试。如果要额外运行官方
`skill-creator` 快速验证器，用环境变量指向单独可用的 `skill-creator` 包；不假设
开发机路径：

```sh
python3 scripts/run_release_gate.py --skill-creator-root "$SKILL_CREATOR_ROOT"
```

最后一条命令的前置条件：`SKILL_CREATOR_ROOT` 所指目录包含
`scripts/quick_validate.py`。门禁不会打 tag、发行、发布、修改 ruleset，也不会操作
用户工作区。
