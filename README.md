# 股权激励管理服务

面向员工股权授予、归属与结算的 Python 后端服务，为人事、薪资与证券管理三个岗位提供同一套可信口径：离职交接与日常归属使用同一引擎，员工可核对每次归属的条件、税费、汇率与到账构成，管理员可追踪未完成的审批、失败回执与离职后仍需处理的批次。

## 领域规则

**批次状态机**：`PENDING 待满足 → LOCKED 锁定 → RELEASABLE 可执行 → SETTLED 已结算`，任一未终结状态可转入 `FORFEITED 失效`。

- **归属计时**：按有效服务天数归属；停薪休假期间暂停计时，归属日相应顺延（顺延后再落入休假段则继续顺延）。
- **绩效条件**：达成率 = 实际值 / 目标值，低于阈值整体失效，高于上限按上限计；部分达标部分归属，未达标部分失效。
- **跨时区截止点**：归属日、截止日均按协议约定时区的自然日界定，换算为 UTC 时刻比较，与执行地点无关。
- **黑窗期**：条件满足时若处于黑窗期，批次进入锁定；黑窗结束后自动解除，结算执行在黑窗期内会被确定性拒绝。
- **协议修订**：只能追加新版本且只能向后生效；已结算批次封存不得倒改，已归属（锁定/可执行）批次权利不得削减。
- **结算方式**：`net_share` 净股交付（公司代扣股份抵税）、`sell_to_cover` 卖股缴税、`gross` 全额交付（税款现金补缴）。抵税股数向上取整保证税款足额，多抵部分现金返还；交付股数向下取整，不足一股以现金补偿。跨境员工按当日汇率折算应税所得。
- **三方复核**：结算批次需人事（服务/休假/离职事实）、薪资（税务身份/预扣）、证券管理（授予/价格/交割）三方确认；任何一方退回时保留原意见与差异（记录值 vs 认定值），修正后重新提交版本号递增，历史不清除。
- **并发结算**：执行分两阶段（先整体校验试算再统一入账），幂等键 + 锁保证同一批次不会重复扣股或重复付款；同一幂等键重放返回原结果。
- **离职处理**：按离职类型应用策略（未归属失效/加速、已归属未结算是否失效、结算窗口天数）；窗口届满仍未结算的批次失效；已离职员工的待处理批次进入管理员追踪视图。

数量与金额一律使用 `Decimal`，金额两位小数（ROUND_HALF_UP）。所有计算的时间显式传入，结果可重现。

## 运行

需要 Python 3.11 或更高版本：

```bash
python3 src/index.py
```

服务默认监听 `8000` 端口，`GET /health` 确认进程状态。成功的变更请求按顺序追加到 `.runtime/commands.jsonl`，重启后自动重放恢复状态。

执行测试：

```bash
python3 -m unittest discover -s tests
```

端到端演示（跨境员工离职结算全流程）：

```bash
python3 demo.py
```

也可以运行 `docker compose up --build` 启动容器。

## 主要接口

所有接口接收/返回 JSON；日期与时间用 ISO 字符串，金额/数量用字符串。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/employees` | 登记员工 |
| POST | `/grants` | 创建授予（协议 v1 与批次约定） |
| POST | `/grants/{id}/amendments` | 协议修订（向后生效的新版本） |
| POST | `/grants/{id}/recalculate` | 按最新资料重算归属 |
| POST | `/grants/{id}/performance` | 登记绩效实际值 |
| POST | `/employees/{id}/leaves` | 登记休假（可暂停归属计时） |
| POST | `/employees/{id}/tax-identity` | 登记税务身份 |
| POST | `/employees/{id}/terminations` | 登记离职（按类型应用策略） |
| POST | `/market-prices`、`/fx-rates`、`/blackouts` | 登记市场价格、汇率、黑窗期 |
| POST | `/batches`、`/batches/{id}/submit` | 创建并提交结算批次 |
| POST | `/batches/{id}/reviews` | 三方复核（同意/退回，含差异） |
| POST | `/batches/{id}/resubmit` | 修正后重新提交（版本递增） |
| POST | `/batches/{id}/execute` | 执行结算（幂等键） |
| POST | `/receipts` | 登记外部回执（券商/薪资/税务） |
| POST | `/maintenance/expire-windows` | 离职结算窗口届满处理 |
| GET | `/employees/{id}/statement?as_of=` | 员工对账单 |
| GET | `/admin/pending-approvals` | 未完成的三方确认 |
| GET | `/admin/failed-receipts` | 失败回执 |
| GET | `/admin/post-termination?as_of=` | 离职后仍需处理的批次 |
| GET | `/tranches/{id}`、`/batches/{id}`、`/ledger/entries` | 批次、结算批次与台账明细 |

业务拒绝返回 `409`（含错误类型），参数错误返回 `400`，未知资源返回 `404`。

## 代码结构

```
src/equity_platform/
  models.py      领域模型与状态机、离职策略
  timeutil.py    跨时区截止点
  vesting.py     归属引擎（服务天数、休假顺延）
  settlement.py  结算试算（净股/卖股缴税/全额、汇率、碎股现金）
  approvals.py   三方复核工作流
  ledger.py      幂等台账
  platform.py    编排核心（版本、重算、离职、执行）
  reports.py     员工对账单与管理员视图
src/app.py       JSON API 与命令日志持久化
tests/           unittest 测试（含并发结算）
demo.py          端到端演示
```
