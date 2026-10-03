# 建筑抗震鉴定与加固排序

依据结构、用途、人员密度和历史缺陷生成鉴定与加固优先级。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量、批次角色与对账差异。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链；状态变更与审计同事务写入。
- `src/ledger.py`：监管台账客户端（按外部编号对账、失败注入、人工并发更新模拟）。
- `src/service.py`：权限检查、用例编排、并发控制、审计、上报批次与证据失效。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败和上报批次测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8317
```

默认端口为`8317`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`
- `GET /api/report-batches`
- `POST /api/report-batches`，body 为 `{"item_ids":[...]}`，按外部编号生成上报批次
- `GET /api/report-batches/{id}`
- `POST /api/report-batches/{id}/confirm`，仅 `review_board`，按外部编号与台账对账
- `POST /api/report-batches/{id}/retry`，上报失败后按同一批次号重试

允许角色：assessor, structural_engineer, review_board, viewer。风险分值和人员密度共同影响排序；审核通过前必须完成评估、设计和施工证据登记。

## 上报批次与对账

鉴定结论不再人工逐条报台账，而是由评估/结构岗把待报项目打成批次，复核委员会确认后上报：

- **按外部编号对账**：批次条目快照本地结论（状态、严重度、优先级、证据数等）与本地版本；上报时按 `external_ref` 与台账回执比对。
- **回执版本不同就保留本地值并列出差异**：台账版本高于本地（人工或并发上报改过）时，条目标记 `diff`，本地快照不改写，并逐字段列出 `{field, local, receipt}` 差异。
- **越权确认被拒绝**：确认/重试仅 `review_board` 可执行，`assessor` 等越权返回 403。
- **证据更新后原上报结论失效重算**：登记证据后，已确认批次中该项目条目标记 `stale`（失效），审计记录 `invalidated_batches`；需重新建批次（重算结论）再上报。
- **失败按同一批次号重试**：上报失败批次置 `failed` 并写审计，`retry` 沿用原批次号；台账按版本幂等接受，已成功的条目不重复覆盖。
- **状态与审计链一起写入**：建项、登记证据、状态流转、批次创建/确认/失败均在同一 SQLite 事务内写状态与审计事件，`GET /api/audit` 可核对 SHA-256 链，不存在"改了项目没写审计"的半成品。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
