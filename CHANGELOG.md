# 更新日志

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [v0.1.0] - 2026-10-31

重写版首版：把 v0.8.0 的能力用新骨架重做。

### 结构

- `core/` 六个模块（config / db / facts / persona / care / state / extract / service / journal）不 import astrbot，可裸跑单测。
- `plugin.py` 只做框架接线；`main.py` 从 1230 行降到约 600 行。
- 配置读取统一走 `Config.load()`，clamp、类型兜底、baseline 合并只写一处。
- 数据库迁移合并成一份 `MIGRATIONS` + `PRAGMA user_version`。
- 设置分组从 `_conf_schema.json` 的 key 前缀推导，不再维护第二份表。

### 修复（都是测试抓出来的）

- 提示词模板里的 JSON 花括号与 `str.format` 冲突导致 KeyError。
- 状态变更（"现在吃香菜了"）被误判为冲突，改为「极性相反且核心词相同」才算冲突。
- 判重不去标点导致同一句话重复入库。
- 人格增量解析放行未知维度。

### 继承自 v0.8.0 的能力

- 结构化事实库（槽位/类型/重要度/置信度/极性/向量）+ 覆盖链。
- 时间配速的人格演化（单次上限 / 每日预算 / 半衰期回归）。
- 主动开口九道闸门 + 拆条发送 + 下次最早时间与依据。
- 插件自有事件日志（按天、可筛、面板第五页签改为第四页签）。
- 面板四页签：记忆 / 人格 / 主动消息 / 日志。
