# 陪伴记忆 · astrbot_plugin_companion

给 AstrBot 的**私聊**加一层长期记忆和一层会缓慢变化的人格。零依赖、全本地、可审计、可撤销。

这是 [astrbot_plugin_memory_evo](https://github.com/1244p/astrbot_plugin_memory_evo) 的重写版：同一套思路重新搭骨架，把上一版长出来的补丁债清掉（`main.py` 从 1230 行拆成接线 + 六个 core 模块，配置读取从 55 处散落的字符串键收成一个入口）。

## 它做什么

- **记住偏好**：聊过的事抽成结构化条目（槽位 + 内容 + 类型 + 重要度 + 置信度），下次自动带上。
- **状态变了会覆盖**：同一槽位说了新值，旧值标 `superseded` 不再召回，历史仍在库里。
- **人格缓慢演化**：五维（亲密/玩心/直率/主动/温度）按**真实时间**配速——单次上限、每日预算、半衰期回归三者同时生效。
- **安静久了会开口**：过闸门判定后自己发一条，附下次间隔；免打扰、允许时段、每日上限、连发没回都会拦住。
- **可审计**：`/memory why` 给每条召回的分数与依据；面板能看指标、人格时间线、闸门状态、插件自己的日志。

## 安装

```bash
cd /AstrBot/data/plugins
git clone https://github.com/1244p/astrbot_plugin_companion
# 重启 AstrBot
```

配置在 WebUI 的插件设置里改，或直接编辑 `data/config/astrbot_plugin_companion_config.json`。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/memory dump [n]` | 看最近记住的东西 |
| `/memory why <文本>` | 这条消息会注入什么、每条为什么被选中 |
| `/memory extract` | 干跑一次抽取：看模型返回什么、哪条会被丢 |
| `/memory extract go` | 立刻抽一次并写库 |
| `/memory forget <id>` | 删除一条（物理删，不复活） |
| `/memory confirm <id>` | 把待确认的冲突条目认下来 |
| `/memory persona` | 五维当前值与基线 |
| `/memory care on\|off` | 这个会话的主动消息开关 |
| `/memory care status\|why\|now` | 状态 / 为什么没发 / 手动发一次 |
| `/memory logs [n]` | 插件自己的事件日志 |

工具：`memory_search`（模型主动查记忆）、`memory_write`（模型主动记一条）。

## 面板

插件详情页 → 打开视图，四个页签：

- **记忆**：按条列出，可搜、按状态/类型筛、行内确认或删除、批量操作、导出。
- **人格**：五维卡（带基线刻度）+ 演化时间线（每次位移与依据）。
- **主动消息**：状态卡（可以发 / 暂不发）+ 闸门逐条 + 上次说了什么。
- **日志**：按级别/关键词筛，含统计与清空。

## 关键机制

### 召回打分

```
score = 0.5 * 相似度 + 0.2 * 新近度 + 0.2 * (重要度/5) + 0.1 * 置信度
```

有向量走余弦，没向量走字面二元组重叠。半衰期按类型：偏好 30 天、身份 60 天、事实 14 天、事件 7 天、在办 1 天。门控：置信度低于阈值且重要度不满 5 的不注入；同槽位最多 `max_per_slot` 条；单条超长截断。

### 人格配速

```
步长 = min(单次上限, 单次上限 * min(1, 距上次演化小时/24), 当日预算余额)
新值 = 旧值 + 步长 * 方向 + (基线 - 旧值) * (1 - 0.5^(小时/(半衰期*24)))
```

默认 0.03 / 0.15 / 7 天：一天不管聊多久，五维累计最多动 15%；偏离 45% 时约 7 天回一半。每天几十次抽取也不会把值顶满。

### 主动开口闸门

按顺序：总开关 → 会话开关 → 免打扰 → 允许时段 → 连发没回 → 空闲够久 → 距上次主动够久 → 今日上限 → 概率。

`/memory care why` 和面板显示的是**同一份判定结果**，不存在「日志说 A 面板说 B」。

## 数据

```
data/plugin_data/astrbot_plugin_companion/
├── memory.db                    # facts / persona / persona_log / care
└── logs/companion-YYYYMMDD.log  # 每行一个 JSON，保留 7 天
```

删除全部记忆：备份 `memory.db` 后删掉 `memory.db*`，重启 AstrBot。

## 开发

```bash
python -m unittest discover -s tests     # 60 条，纯 stdlib，不需要装 AstrBot
```

`core/` 不 import astrbot——调模型、算向量、发消息都是注入的回调，所以业务逻辑可以裸跑测试。`plugin.py` 是唯一认识框架的文件。

## 许可

MIT
