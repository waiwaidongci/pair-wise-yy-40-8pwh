# 建筑抗震鉴定与加固排序

依据结构、用途、人员密度和历史缺陷生成鉴定与加固优先级。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/ledger.py`：监管台账适配器（按批次号幂等、按外部编号对账，可注入故障）。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

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

### 监管台账批次上报

鉴定结果不再人工逐笔填报，而是组成批次由复核委员会确认后整体上报：

- `POST /api/report-batches`：创建批次，提交`{"external_refs":[...]}`（鉴定员/结构工程师可建）。
- `POST /api/report-batches/confirm`：确认上报，仅`review_board`可调用；鉴定员越权确认返回403。
- `POST /api/report-batches/retry`：上报失败后按**同一批次号**重试（仅失败批次可重试，幂等）。
- `GET /api/report-batches`、`GET /api/report-batches/{batch_no}`：查询批次与对账结果。

规则与保证：

- 按`external_ref`对账：回执版本与本地不一致时保留本地结论，条目置`mismatch`并在`diffs`中逐字段列出差异；台账缺失该编号置`missing`。
- 未确认（draft）或失败（failed）批次会锁定其外部编号，同一项目不能同时进入两个批次；确认/重试以`BEGIN IMMEDIATE`事务串行化，并发确认只有一个成功，其余409。
- 证据更新（新增record）后`evidence_version`与项目`version`递增，已上报结论置`report_stale=true`并写`report_invalidated`审计；下次批次按最新证据重算结论。
- 项目上报状态、批次条目结果、审计事件在同一个SQLite事务提交；台账传输不可用时批次置`failed`、写`report_batch_failed`审计（返回502），项目状态不产生任何变更，不会留下改了项目没写审计的半成品。

允许角色：assessor, structural_engineer, review_board, viewer。风险分值和人员密度共同影响排序；审核通过前必须完成评估、设计和施工证据登记。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
