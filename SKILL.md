---
name: agent-self-evolution
description: 让 Agent 从「反复犯同一个错」变成「真的越用越强」。扫描 feedback/日志/教训源，区分「事故 vs 条款」，自动归因根因，纵向追踪事故数是否真的下降，并把经验落成带版本号的策略条目。适用于任何有 feedback 沉淀的 Agent 系统（OpenClaw / Claude Code / Codex / Cursor）。
version: 0.3.2
license: MIT
---

# Agent 自进化（Self-Evolution）工程包

> 不是"让 Agent 写反思日记"，是**把经验变成下次必然撞上的闸门**。

## 触发条件（When to use）

- 你发现 Agent **同一个错反复犯**（"这个我不是说过吗"）
- 你想让 Agent **做完 1000 次任务比第一次强**
- 你已经有 feedback / 日志 / 教训沉淀，但**不知道它有没有真的变好**
- 你要给 Agent 建**可持续进化的策略层**（不是写死的 prompt）

## 执行策略（How）

### 核心判据（先记住这一句）

> **文档型 update ≠ 进化。** 写进 SOP、靠"下次记得读" = 记日记。
> **闸门型 update 才是进化**：改结构 / 装脚本 / 加断言，让下次**必然撞上**。

实证：某红线一天内被违反 **4 次**，而该条款早就写在 SOP 里。

### 三步走

**Step 1 · 扫描：区分「事故」与「条款」**

```bash
python3 scripts/recurrence_scan.py --config config.json
```

关键设计：**事故**（真出过事）和**条款**（只写了规矩）分开统计。
→ 条款写了 8 条、事故仍 6 次 = **文档型 update 失效的量化铁证**。

**Step 2 · 归因（诊断与改写必须分离）**

```bash
python3 scripts/recurrence_scan.py --config config.json --attr
```

> ⚠️ 依据 survey 结论：**先把诊断做对，再改**。
> SkillForge / CoEvoSkills 先用验证器做根因分析再针对性改写，跨任务效果**显著优于**
> AutoSkill / SkillClaw 那种"直接在原始轨迹上让模型自己改"——后者会把任务特异性的偶然行为
> 当成可复用模式。

输出：每个复发病灶的 **根因 + 建议闸门 + 置信度**。不自动改文件。

**Step 3 · 纵向追踪（补行业公认盲区）**

```bash
python3 scripts/recurrence_scan.py --config config.json --trend
```

> survey 对 10 个评估基准的分析发现**三大盲区**，第一条就是：
> **没有任何 benchmark 追踪技能在多轮反馈后是否真正变好**——只能知道"这轮过没过"。
>
> 本步骤输出：本期 vs 上期的**事故数变化**，以及**闸门安装前后的对比**。
> 装了闸门到底有没有用？用数据说话，不靠感觉。

### 策略层（Policy）

经验落成 `policy/` 下带版本号的条目，**每条必须四要素齐全**（缺一不可）：

```markdown
## P-003 收到修改意见不得直接动笔  v2
- 触发条件：用户在任何创作/写作场景给出修改意见
- 执行策略：先复述理解 → 提问挖意图 → 自评把握度 ≥95% → 等确认 → 再动笔
- 终止条件：用户明确点头
- 复用接口：所有写作类任务通用
- 版本：v2（v1 失效，因一天内被违反 4 次 → 升级为 hook 级拦截）
- 效果验证：事故数 6 → 待观察（关联报告 recurrence-report-YYYY-MM-DD.md）
```

**策略演进三条路径**（借鉴 Darwin Skill）：
- **FIX**：就地修补失效条目
- **DERIVED**：从父策略派生专精变体
- **CAPTURED**：从一次成功执行抽取全新策略

**改动原则**：diff 级最小改动，可回滚；版本用 DAG 记谱系。

## 终止条件（When to stop）

- 该类病灶事故数**连续 2 期为 0** → 标记为已闭环，转入 COLD
- 归因置信度 < 0.5 → **不改**，只记录（防止把噪声当模式）
- **安全类条目（凭据/权限/删除/外发）永远不自动改**，必须人工批准

> survey 盲区三：**安全只做一次性检查**。不受控的技能自进化可能悄悄剥离已有安全约束。
> 所以本包对安全类策略硬编码"人工批准"闸门。

## 复用接口（Integration）

- 输入：任意文本源（feedback 日志 / SOUL.md / AGENTS.md / checkpoints）
- 输出：Markdown 报告 + 策略条目 + 可选 DM 通知
- 依赖：Python 3 标准库；LLM 归因可选（缺失时降级为不归因，明确标注）
- 适配：OpenClaw / Claude Code / Codex / Cursor 均可（只需改 config.json 里的路径）

## 安装

```bash
git clone https://github.com/Oooooldbai/agent-self-evolution.git
cd agent-self-evolution
cp templates/config.example.json config.json   # 改路径
python3 scripts/recurrence_scan.py --config config.json
```
