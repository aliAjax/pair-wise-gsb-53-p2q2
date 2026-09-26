# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、法定天数、补件期限、材料完整性，以及亲属/共同公司的利益冲突核验。
- `src/repository.py`：SQLite建表、事务和查询（案件、审计、人员资料、指派记录）。
- `src/service.py`：用例编排、权限检查、乐观并发、审计，以及收案/换人的指派与复核工作流。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面（含人员维护、收案指派、复核、换人、重新核验操作）。
- `tests/`：完整流程、规则计算、失败场景与利益冲突专项测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表。

## 利益冲突核验

代理人指派不再只看案件备注。人员资料中记录与申请人/担保人的**亲属关系**和**共同公司**：

- 收案（`POST /api/records`）可在`data`中直接携带`lead_person_id`（主办律师）、`assistant_person_id`（协办）、`sponsor_id`及`applicant_company_ids`/`sponsor_company_ids`。
- 指派律师或协办前，系统先比对人员资料与案件当事人：
  - 命中亲属关系或共同公司 → 指派状态停在`pending_review`，案件所有业务动作被拦截（409）。
  - 主管（`supervisor`）必须调用复核接口写明理由，`approved`放行或`rejected`驳回；驳回后案件仍不可办理，必须换人。
- 换人（`POST /api/records/{id}/assignments`）后按新人员重新检查；原指派记录保留（`removed`或`rejected`），复核意见全程留痕。
- 人员关系更新或案件重开后，可对任一进行中/待复核指派调用`recheck`，按最新资料重新核对：active命中新冲突会回到待复核；待复核即使冲突已解除也需主管重新放行。
- 案件详情（`GET /api/records/{id}`）返回完整`assignments`：席位、人员、冲突来源（`conflicts[].description`）、处理状态（`active/pending_review/rejected/removed`）和历次放行依据（`reviews`）。
- 不携带指派字段的普通收案/办理流程与原来完全一致。

人员资料接口：

- `POST /api/personnel`（主管）：`{"data":{"person_id":"LAW-1","name":"...","relatives":[{"other_party_id":"A-900","other_party_kind":"applicant","relation":"夫妻"}],"companies":[{"company_id":"CO-1","company_name":"...","relation":"股东"}]}}`，全量覆盖。
- `GET /api/personnel`、`GET /api/personnel/{person_id}`：查询人员及其关系。

指派接口：

- `POST /api/records/{id}/assignments`：`{"data":{"role":"lead|assistant","person_id":"..."}}`，收案人员或主管可用。
- `POST /api/assignments/{id}/review`（主管）：`{"data":{"decision":"approved|rejected","reason":"必须填写"}}`。
- `POST /api/assignments/{id}/recheck`：按最新人员资料重新核验。
- `GET /api/records/{id}/assignments`：指派历史。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情（含指派与冲突复核信息）。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及亲属/共同公司冲突命中、待复核拦截、主管放行/驳回、换人重新核验、重开核对和审计留痕。
