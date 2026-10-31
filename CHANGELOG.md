# 更新日志

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [v0.1.2] - 2026-10-31

面板打开是空的。查下来不是前端画得不全，是后端一条接口都没注册上。

### 修复

- `Routes.register()` 从没被调用：`main.py` 只构造了 `Routes(self)`，框架里的
  `registered_web_apis` 因此是空表，面板每个请求都回「未找到该路由」。现在构造完立刻注册。
- 注册路径多了一层 `api`：框架用 `"/" + subpath` 对注册路径做 `re.fullmatch`
  （`dashboard/api/plugins.py:209`），而 bridge 送来的 subpath 是 `<插件名>/scopes`，
  所以前缀必须是 `/<插件名>`，原来写成 `/<插件名>/api` 永远匹配不上。
- `[hidden]` 不生效：`.bulkbar` / `.panel` 自带 `display`，盖掉了浏览器默认的
  `[hidden] { display: none }`，导致四个页签的表格同时铺出来、批量删除条常驻显示「已选 0 条」。
  加一条全局 `[hidden] { display: none !important }`。
- 日志页时间列一直是空的：前端读 `row.time`，而 `core/journal.py` 写的是 `ts`（epoch 秒）。
  改用 `timeText(row.ts)`，并把日志行从 flex 改成 grid —— flex 下每列宽度随内容浮动，列根本对不齐。
- 人格时间线会把 −0.4% 显示成「直率 0」，小于半个百分点的位移现在直接不显示。

### 新增

- `tests/test_routes.py`（15 条）：断言 10 条路由都注册在 `/<插件名>/<子路径>`、路径里没有
  `/api/` 段、**`views/companion/app.js` 里出现的每个 endpoint 都能在注册表里找到**
  （前后端名字漂了立刻红），以及逐个 handler 跑通读、写、过滤、开关切换。
- `scripts/preview_server.py`：脱离 AstrBot 也能看面板，`?tab=persona&dark=1` 切换页签与主题，
  样例数据与线上接口同形状。开发用，不打包。

## [v0.1.1] - 2026-10-31

首版装上就报「neither main.py nor …; skipping it」，本轮修掉两处只在真机才暴露的问题。

### 修复

- 入口文件名 `plugin.py` → `main.py`：AstrBot 的 `StarManager._get_modules`
  只认 `main.py` 或与目录同名的 `<dir>.py`，其余一律 skip，插件装进目录却永远不加载。
- `from astrbot.api.message import MessageChain` 这个模块不存在（`astrbot/api/` 下只有
  `message_components.py`），改为从 `astrbot.api.event` 导入。

### 新增

- `scripts/build_zip.py`：一键出可安装 zip，固定时间戳、排除测试与缓存。
- `tests/test_packaging.py`：按框架源码复刻打包规则自检（顶层目录、metadata 必需字段、
  入口文件、开发文件不打包、配置项必须落在分组里），以后打包问题在 CI 就能挡下。

## [v0.1.0] - 2026-10-31

重写版首版：把 v0.8.0 的能力用新骨架重做。

### 结构

- `core/` 六个模块（config / db / facts / persona / care / state / extract / service / journal）不 import astrbot，可裸跑单测。
- `main.py` 只做框架接线；业务逻辑都在 `core/`。
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
