# 04-checklane（检道）

Checklane — 数据质检通道：入检批次装载 → 规则命中与严重度裁决 → 隔离/放行签发 → 签发清单与源批对账。

**不是** Feed 多源归并（Feedstitch），**不是** 作业依赖编排（Batchgate），**不是** CDC 变更流按序落库（Changeflow）。
**也不是** 报表看板 / CRUD 后台 / 电商订单 / OA 工单。

## 启动

`ash
docker compose up --build
`

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5400 |
| API | http://localhost:10400 |

本地开发（可选）：

`ash
# backend
cd backend && pip install -r requirements.txt
set DATA_DIR=..\\data
uvicorn app.main:app --reload --port 10400

# frontend
cd frontend && npm install && npm run dev
`

## 主链

登记数据集与规则 → 装载夹具批次入检 → 按规则裁决放行/隔离 → 签发到精选区 → 批次报告与各列表数字同源

## 种子说明

- **Orders Snapshot**：JSONL 商品快照，含必填缺失与类型错误行，用于演示入检与隔离。
- **Inventory Snapshot**：库存快照，独立数据集，用于第二路规则演示。

打开「入检台」选数据集跑一次，再看隔离区 / 精选区 / 批次报告计数是否与本次报告一致。

## 同键变更门禁（prior_curated_drift_guard）

入检只看本批行与规则、不读精选存量时，同键行改了字段也会被当新行无审计盖掉精选。本门禁在入检主链上加了一层精选对照：

1. **入检开始冻结快照**：按本数据集当前精选逐键冻结到 `drift_guard_snapshot`，全程只对照快照。键不在快照 → 首次键，走普通规则；键已在 → 对可配置比对字段集做变更判定（任一比对字段值变化即变更，留空表示全字段）；**空 entity_key 即使规则通过也不得进入精选**。
2. **策略互斥可配**（同时只生效一种，存 settings，刷新/重启仍在，支持全局与按数据集覆盖）：
   - `block` 阻断（默认）：变更行规则虽通过也改判进隔离区，且不得写入/覆盖精选，精选保持快照旧值；
   - `overwrite` 覆盖：允许本批通过行覆盖精选，但每次覆盖在 `curated_change_audit` 留下变更前精选值与变更后值的逐字段审计。
   - 同键未变更行按原规则处置，不受策略影响。
3. **报告与对账**：批次报告区分首次键 / 同键未变更 / 同键变更（阻断隔离数、覆盖数）/ 空键未签发，并附带快照冻结、签发数与分类互证、阻断行未触碰精选、覆盖审计前后值、空键未入精选等对账校验；行清单带门禁分类与变更字段。
4. **页面**：「同键变更门禁」页可配比对字段与互斥策略、查看覆盖审计明细与各批冻结快照；「入检台」行清单展示门禁分类与变更字段。

接口：`GET/PUT /api/drift-guard/config`（`dataset_id` 省略为全局）、`GET /api/drift-guard/audit`、`GET /api/drift-guard/snapshots/{batch_id}`。

本机演示（SQLite + 夹具，数据集 3 *Curated Drift Demo*）：

```bash
# 1. 基线夹具先签发建精选（4 个首次键）
curl -X POST localhost:10400/api/inspect -d '{"dataset_id":3}'
# 2. 把数据集夹具换成 curated_drift_snapshot.jsonl（G-2 改 price、G-3 改 sku、G-5 新键）
#    默认 block：G-2/G-3 进隔离，精选不动
curl -X POST localhost:10400/api/inspect -d '{"dataset_id":3}'
# 3. 切 overwrite 再跑：覆盖精选并产生 before/after 审计
curl -X PUT localhost:10400/api/drift-guard/config -d '{"strategy":"overwrite"}'
curl -X POST localhost:10400/api/inspect -d '{"dataset_id":3}'
curl "localhost:10400/api/drift-guard/audit?dataset_id=3"
```

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 0-1 切口（未实现，见题单）

当前仓库已实现「单批入检 + 基础规则 + 放行签发」主链与「精选同键变更门禁」（见上节）。严重度整批阻断、规则包版本、抽样入检、跨字段规则、批次重开、金标对照、批内重复键、放行冻结窗、规则临时豁免、跨数据集参照检查、影子规则包、画像基线门禁、金丝雀两阶段签发等仍在 backend/app/modules/* 留空包，按题单逐题换主链路实现。
