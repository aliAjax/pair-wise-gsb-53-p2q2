# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、法定天数、补件期限、材料完整性和利益冲突核验。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景和利益冲突测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，`data`可选`sponsor_ids`（担保人ID列表）。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `GET/POST /api/persons`：人员资料列表 / 新增或更新，POST体为`{"person_id":"L-1","data":{"name":"李律师","relative_ids":["A-1"],"company_ids":["CO-X"]}}`。
- `GET /api/persons/{id}`：人员详情。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 利益冲突核验

人员资料记录两类关系：`relative_ids`（亲属，单向登记即双向生效）与`company_ids`（共同任职公司）。

- `assign`（收案，draft状态；intake_officer/supervisor）：指派`legal_rep`律师或`assistant`协办前，系统自动比对被指派人与申请人、担保人的亲属关系和共同公司。命中则案件停在`conflict_review`（待复核），普通流程动作全部被状态机挡住。
- `release_conflict`（仅supervisor）：必须提交`release_reason`写明放行理由，可附`review_opinion`，放行后案件返回进入复核前的状态。
- `reject_conflict`（仅supervisor）：必须提交`reject_reason`，驳回后返回原状态，原指派保留。
- `reassign`（换人）：再次执行完整核验；命中重新进入待复核。待复核中也可直接换人（旧待复核标记为`replaced`）。原指派记录不被覆盖。
- `reopen`（仅supervisor，closed状态）：重开时对当前代理人重新核验，命中同样停在待复核。

案件详情的`conflict`字段展示：`status/under_review`处理状态、`pending`待复核项及其`conflict_sources`冲突来源（`kinship`/`shared_company`及对方姓名、公司ID）、`current`当前代理人、`releases`放行依据（理由、复核意见、放行人）和`history`全部指派/复核历史。完整payload中的`assignments`保留每一轮原指派与复核意见。

不使用指派时原有收案与流程完全不变。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
