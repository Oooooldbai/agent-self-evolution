# agent-self-evolution

**让 Agent 从「反复犯同一个错」变成「真的越用越强」。**

不是一个"让 Agent 写反思日记"的 prompt，是一套**工程包**：扫描 → 归因 → 纵向追踪 → 落成带版本的策略，再加一层 **Auto Dream 记忆巩固**（Claude Code L4 规格）。

[English below](#english)

---

## 为什么需要它

绝大多数 Agent 干了 1 万次活，能力和第一次**没区别**——每次从头再来，昨天的坑今天继续踩。

但更常见的情况是：**你写了教训、改了 SOP，然后它继续犯。**

实证（本项目起源环境，2026-10-12）：
- 某条红线在**一天内被违反 4 次**（09:15 / 14:09 / 16:17 / 17:28），而该条款**早就写在 SOP 里**
- 某类问题：**条款写了 8 条，事故仍然 6 次**

结论只有一个：

> **文档型 update ≠ 进化。** 写进 SOP、靠"下次记得读" = 记日记。
> **闸门型 update 才是进化**：改结构 / 装脚本 / 加断言，让下次**必然撞上**。

## 它做什么

| 步骤 | 命令 | 产出 |
|---|---|---|
| 1 扫描 | `--config config.json` | 每个病灶的**事故数 vs 条款数** |
| 2 归因 | `--attr` | 根因 + 建议闸门（**只诊断，不改文件**） |
| 3 追踪 | `--trend` | 本期 vs 上期事故数变化 |
| 4 落策略 | 人工按模板写 | `policy/` 下带版本号的四要素条目 |
| 5 记忆巩固 | `auto_dream.py --config config.json --attr` | Auto Dream 报告（**触发闸 + 三重门槛 + 五动作，只出建议**） |
| 6 记忆切片 | `memory_draft.py --config memory.config.json` | digest → `drafts/` 素材切片 + 待提炼清单（**只搬素材，不写记忆**） |

### 关键设计 1：区分「事故」与「条款」

只数问题会自我膨胀（谈得越多命中越高）。分开统计后，机器能自己判读：

> 条款 8 条、事故 6 次 → **"文档型失效"**

这是"文档型 update 无效"第一次有了**量化证据**，而不是靠感觉。

### 关键设计 2：诊断与改写分离

来自 Rutgers & UNC Charlotte 的 survey（19 种技能进化方法）：

> 先用验证器做根因分析、再针对性改写的系统（SkillForge / CoEvoSkills），
> 跨任务效果**显著优于**"直接在原始轨迹上让模型自己改"的（AutoSkill / SkillClaw）。
> 原因：原始轨迹噪声太多，模型容易把**任务特异性的偶然行为**当成可复用模式。

所以 `--attr` **只输出诊断，绝不自动改文件**。

### 关键设计 3：纵向追踪（补行业公认盲区）

该 survey 分析 10 个评估基准后发现三大盲区，第一条就是：

> **没有任何 benchmark 能追踪技能在多轮反馈后是否真正变好**——你只能知道"这轮过没过"。

`--trend` 输出事故数的**期际变化**，用数据回答：**装了闸门到底有没有用**。

### 关键设计 4：安全类永不自动改

survey 盲区三：安全只做"进门检查"，而**不受控的技能自进化可能悄悄剥离已有安全约束**。

本包对**凭据 / 权限 / 删除 / 外发**类策略**硬编码人工批准**。

### 关键设计 5：Auto Dream 触发闸（24h + ≥5 会话才跑）

补 Claude Code 记忆四层里缺的 L4「Auto Dream」——后台大扫除：**合并重复 / 删无效 /
提炼核心模式 / 优化索引 / 跨会话洞察**，只出报告不自动改。

- **触发闸（KAIROS）**：距上次 Dream **≥24h** 且窗口内 **≥5 个新会话**才真跑。
  后台每小时探测一次，到没到点由脚本自己判——未到点 `exit 3`、信号源没配 `exit 2`，
  都是**显式拒绝并打印数字**，绝不静默假装跑过；手动点火用 `--force`，只判闸用 `--gate-only`。
- **三重晋升门槛（Dreaming）**：召回次数 / 查询多样性 / 来源可信度
  → 🟢 晋升候选 / 🟡 观察 / ⚪ 沉默（降级候选）。信号双轨：`memory_recall.py` 记的
  **recall-log**（权威）+ 会话日志提及近似（兜底）；两者皆无 → 该节显式降级标注，**不拿 0 当结论**。
- **状态**：`<out_dir>/auto-dream.state.json`——**报告成功写出后才推进水位**（没跑成不消耗触发机会，防假绿）。

```bash
python3 scripts/auto_dream.py --config config.json --attr      # 过闸才跑
python3 scripts/auto_dream.py --config config.json --gate-only # 只判触发闸
python3 scripts/memory_recall.py --config config.json log --id topic.md --query "检索词"
```

> ⚠️ 从 ≤v0.3.x 升级的部署**必须补配** `dream.trigger.session_globs`（会话信号源路径），
> 否则脚本以 `exit 2` 拒绝跑——信号源配错 = 结论全错，宁可拒绝也不静默放行。

## 安装

```bash
git clone https://github.com/Oooooldbai/agent-self-evolution.git
cd agent-self-evolution
cp templates/config.example.json config.json
```

编辑 `config.json`：

```json
{
  "sources": ["~/your-agent/memory/feedback-log.md", "~/your-agent/AGENTS.md"],
  "out_dir": "~/your-agent/checkpoints/memory",
  "policy_dir": "~/your-agent/policy",
  "llm_cmd": "openclaw infer model run --model <your-model>",
  "notify_cmd": ""
}
```

跑：

```bash
python3 scripts/recurrence_scan.py --config config.json --all
```

建议挂 cron（复发检测每周一次）：

```cron
0 9 * * 0 /usr/bin/python3 /path/to/scripts/recurrence_scan.py --config /path/to/config.json --all --notify
```

Auto Dream 建议**每小时探测**（闸门自判到点；未到点 `exit 3` 属正常，不是故障）：

```cron
0 * * * * /usr/bin/python3 /path/to/scripts/auto_dream.py --config /path/to/config.json --attr
```

## 与其他方案的区别

| | 常见 self-improving skill | 本包 |
|---|---|---|
| 提升终点 | memory.md / CLAUDE.md（**文档**） | 脚本 / 断言 / 结构改动 |
| 归因 | 无（只记） | 有，**且与改写分离** |
| 效果验证 | 无 | **纵向追踪期际变化** |
| 安全 | 无 | 安全类硬编码人工批准 |
| 策略格式 | 自由文本 | 四要素 + 版本号 + 效果验证 |

> 按 Agent 自进化五层（Memory / Policy / Skill / Tool / Model）对号入座：
> 多数方案停在 **L1 记忆**，本包目标是 **L2 策略**，Auto Dream 补的是 **L4 记忆巩固**。

## 目录

```
SKILL.md                     Agent Skill 标准格式（触发/策略/终止/复用接口）
scripts/recurrence_scan.py   核心脚本（扫描+归因+追踪）
scripts/auto_dream.py        Auto Dream（L4 记忆巩固：触发闸 + 三重门槛 + 五动作）
scripts/memory_draft.py      L2 写入通道（digest → drafts 素材切片 + 待提炼清单）
scripts/memory_recall.py     recall-log 协议（三重门槛的权威召回信号）
scripts/gate.py              通用出口断言框架
templates/config.example.json
templates/policy-entry.md    策略条目模板（四要素）
docs/设计依据.md              survey 要点 + 实证数据 + 迭代记录
```

## English

Most agents do 10,000 tasks and are no better than on day one. Worse: you write the lesson
into the SOP, and they keep making the same mistake.

This repo is an engineering kit, not a prompt:
1. **Scan** — separate *incidents* (actually happened) from *rules* (just written down).
   "8 rules vs 6 incidents" is machine-readable proof that document-style updates don't work.
2. **Attribute** — root-cause analysis only, never auto-edits (diagnosis is separated from
   rewriting, per the Rutgers/UNC Charlotte survey).
3. **Track** — period-over-period incident deltas. No existing benchmark tracks whether a
   skill actually improves across feedback rounds; this fills that gap.
4. **Policy** — versioned entries with four mandatory fields, diff-level changes, rollback.
5. **Dream** — Claude-Code-L4-style memory consolidation (merge duplicates / drop stale /
   distill patterns / refresh index / cross-session insights), gated to run only after
   **24 hours AND 5 new sessions**; report-only, never auto-edits.
6. **Draft channel** — `memory_draft.py` mechanically slices new session digests into
   `drafts/` plus a "to distill" list, so L2 memory never depends on *remembering to write*;
   the semantic step (what becomes memory) stays human-approved.

MIT. PRs welcome.
