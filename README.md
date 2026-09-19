# 股权激励管理服务

面向员工股权授予、归属与结算的 Python 后端服务。接收授予协议、服务期间、绩效条件、
停薪休假、离职类型、黑窗期、市场价格与税务身份等资料,计算每个归属批次从
**待满足 → 锁定 → 可执行 → 已结算 / 失效** 的全过程,并保证:

- **口径唯一**:离职交接与日常归属走同一套领域引擎,结算单可逐项复算;
- **协议版本化**:修订只追加新版本、向后生效;批次归属时锁定管辖版本,已结算批次不得倒改;
- **结果确定**:跨时区截止点、部分达标、净股交付、卖股缴税、不足一股现金补偿均有确定规则;
- **三岗分责**:人事(服务/休假/离职)、薪资(税务身份)、证券管理(协议/数量/价格)分别确认
  自己负责的数据快照;退回保留原意见,重新确认自动附字段级差异;
- **并发安全**:结算幂等,同一幂等键重复提交返回原回执,不会重复扣股或付款;
  失败回执不入台账,可换新键重试;
- **可追踪**:员工可核对每次归属的条件、税费、汇率与到账构成;管理员可追踪待审批、
  失败回执与离职后仍需处理的批次。

## 运行

需要 Python 3.11 或更高版本,仅依赖标准库:

```bash
python3 src/index.py          # 默认监听 8000,持久化写入 .runtime/
python3 scripts/demo.py       # 端到端演示:跨境员工离职结算全场景
python3 -m unittest discover -s tests
```

也可以运行 `docker compose up --build` 启动容器。

## 结构

```
src/
  app.py               HTTP 接口层(JSON)
  index.py             进程入口
  equity/
    enums.py           批次状态机与业务枚举
    constants.py       精度与舍入约定
    timeutil.py        跨时区截止点换算
    models.py          授予/协议版本/批次/休假/离职/税务身份
    pricing.py         市价与汇率 as-of 快照
    tax.py             累进预扣
    vesting.py         归属引擎(休假停计时/部分达标/离职政策/黑窗)
    settlement.py      结算引擎(净股/卖股缴税/零股现金)
    approvals.py       三岗位确认与退回留痕
    store.py           仓储:锁、幂等键、台账、事件日志、.runtime 持久化
    gateway.py         证券过户与付款通道(可注入失败)
    service.py         EquityService 门面
    reports.py         员工对账单与管理员视图
tests/                 46 个单元与集成测试
scripts/demo.py        场景演示
```

## HTTP 接口摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/grants` | 创建授予(首版协议) |
| POST | `/api/grants/{id}/amendments` | 协议修订(向后版本) |
| POST | `/api/grants/{id}/leaves` | 登记停薪休假 |
| POST | `/api/grants/{id}/performance` | 绩效评定 |
| POST | `/api/grants/{id}/termination` | 离职登记 |
| POST | `/api/grants/{id}/evaluate` | 推进批次状态 |
| GET | `/api/grants/{id}` | 授予详情 |
| POST | `/api/tax-profiles` | 税务身份 |
| POST | `/api/market/prices` `/api/market/fx` `/api/market/blackouts` | 市价 / 汇率 / 黑窗 |
| POST | `/api/tranches/{id}/reviews` | 岗位确认或退回 |
| POST | `/api/tranches/{id}/settle` | 发起结算(幂等键) |
| POST | `/api/receipts/{id}/retry` | 失败回执重试 |
| GET | `/api/employees/{id}/statement` | 员工对账单 |
| GET | `/api/admin/dashboard` | 管理员追踪视图 |

请求与响应均为 JSON;金额与股数以字符串表示,日期为 ISO 格式。
错误响应形如 `{"error": {"code": "...", "message": "..."}}`,
`code` 稳定可供调用方分支处理(如 `already_settled`、`not_exercisable`、
`settled_tranche_immutable`、`retroactive_amendment`)。

## 确定性规则要点

- 时间:内部一律 UTC;业务截止日按授予时区日末换算;截止点落在黑窗内顺延至下一开放时刻;
- 数量:归属股数 4 位小数向下舍入;交付整股向下取整,零股按归属日市价折现金;
- 税费:按税务身份累进计算,币种不同时以同一汇率快照双向换算并记录在回执上;
- 扣股:净股/卖股均按"覆盖税额的最小整股"扣减,多扣或剩余部分以现金找零。
