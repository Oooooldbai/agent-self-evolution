---
created: 2026-10-16
---

# AGENTS.md — agent-self-evolution

## 项目简介
让 Agent「越用越强」的工程包：扫描（事故 vs 条款）→ 归因（诊断与改写分离）→ 纵向追踪 →
带版本策略；外加 **Auto Dream 记忆巩固**（Claude Code L4 规格：24h + ≥5 会话触发、
三重晋升门槛、只出报告）与通用出口断言 `gate.py`。

## 目录结构
```
SKILL.md                     Agent Skill 标准格式（触发/策略/终止/复用接口），frontmatter version 必须与最新 commit 同步
scripts/recurrence_scan.py   核心脚本（扫描+归因+追踪）
scripts/auto_dream.py        Auto Dream（L4 记忆巩固：触发闸 + 三重门槛 + 五动作）
scripts/memory_recall.py     recall-log 协议（三重门槛的权威召回信号）
scripts/gate.py + gates/     通用出口断言框架
templates/config.example.json / policy-entry.md
docs/设计依据.md              外部证据 + 实证 + 设计取舍 + 迭代记录（版本真源）
reports/                     报告产出（按需创建）
```

## 开发命令
```bash
python3 scripts/recurrence_scan.py --config config.json --all
python3 scripts/auto_dream.py --config config.json --attr      # 过闸才跑；exit 3=未到点（正常）
python3 scripts/auto_dream.py --config config.json --gate-only # 只判触发闸
python3 scripts/memory_recall.py --config config.json list
python3 scripts/gate.py --list
git add -A && git commit && git push                           # 常规提交（老白 2026-10-10 授权免审）
```

## 代码规范
- 纯 Python 3 标准库，零第三方依赖（压缩会话解压走外部 `zstd` 命令，缺失时显式降级）。
- **诊断与改写分离**：所有脚本只出报告，不自动改文件；安全类永不自动改。
- **显式降级，不静默跳过**：LLM / 信号源 / 读取失败必须在报告里标注原因。
- 触发闸与门槛阈值是规格不是调参：改阈值先改 `docs/设计依据.md`（并 bump SKILL.md version），再改代码。
- 时间/单位：state 用毫秒，比较前统一到秒（v0.4.0 踩过：毫秒当秒 → 间隔负数、新会话恒 0）。

## 注意事项
- 不引入第三方依赖；`config.json` 已在 .gitignore，不提交本机路径配置。
- 改 `auto_dream.py` / `recurrence_scan.py` 后必须用本地夹具跑通三路再提交：
  闸门放行（exit 0）/ 闸门拦截（exit 3 且状态不推进）/ 信号源缺失（exit 2）。
- 红线（须问老白）：删除文件或改 git 历史、`push --force` / `rebase` / `reset --hard`、公开发布。
