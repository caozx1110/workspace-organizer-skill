# Clean-slate 用户指南

这套模式把 Obsidian 当作日常驾驶舱，把 Chat/Agent 当作操作层，把 Markdown
当作唯一事实来源。你每天主要看少量页面，而不是浏览所有任务文件。

## 每天看什么

1. 打开 `01_导航/HOME.md`，再打开 `00_总览/TODAY.md`。
2. 在 `01_导航/FOCUS.md` 手动保留 1–3 个任务链接。
3. 按 TODAY 中的 `Next` 行行动；需要完整队列时才打开 `NEXT.md`。
4. 打开 `WAITING.md` 检查需要他人或外部事件的项目，打开 `INBOX.md` 处理少量新输入。
5. 晚上明确完成/取消任务并写结果；未完成事项只更新下一步或日期。

页面职责：

| 页面 | 谁维护 | 你在这里看什么 |
| --- | --- | --- |
| `HOME.md` | 你 | 入口、习惯和常用表达 |
| `FOCUS.md` | 你 | 今天真正要推进的 1–3 个任务 |
| `TODAY.md` | 生成器 | 焦点、逾期、今日安排、下一步和 Inbox 信号 |
| `NEXT.md` | 生成器 | 全部开放任务 |
| `INBOX.md` | 生成器 | 尚未决定归属的 Capture |
| `WAITING.md` | 生成器 | waiting/blocked 和 follow-up |
| `ARCHIVE_INDEX.md` | 生成器 | 已关闭任务的可读归档导航 |

## 用户端命令

```sh
WO=skill/workspace-organizer/scripts/clean_slate.py
python3 "$WO" init /path/to/vault                 # 先看 preview
python3 "$WO" init /path/to/vault --yes
python3 "$WO" task create /path/to/vault --title "更新护照" --outcome "拿到受理凭证" --yes
python3 "$WO" capture create /path/to/vault --text "供应商发来合同" --yes
python3 "$WO" views generate /path/to/vault
```

Capture 不等于 Task。需要把输入变成任务、挂接文件或放入资料库时，先运行
`capture triage`，查看计划，再用 `approve --yes` 和 `capture triage-apply`。
归档同理：先明确 `task complete` 或 `task cancel`，再 `archive plan`、精确批准、
应用和验证。误归档用 `restore plan/apply`，不会静默搬回。

## Obsidian 中的编辑边界

你可以直接编辑 Task 正文、日记和 HOME/FOCUS。Agent 只用 CAS 更新核心属性，
因此 Obsidian 在 Agent 读取后保存会产生冲突，而不是被覆盖。生成器只覆盖带有
自身 marker 的五个总览页；HOME 和 FOCUS 永远保留。

不确定 owner、敏感度、目标或意图时，保持在 Inbox，不让模型猜。默认视图最多
显示 `internal`，更敏感内容不会出现在标题、路径、计数或排序中。
