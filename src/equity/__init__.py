"""股权激励归属与结算平台领域核心。

模块划分:
- enums / constants: 状态机、角色、精度约定
- models: 授予、协议版本、批次、休假、离职、税务身份等事实
- timeutil: 跨时区截止点换算
- pricing: 市场价格与汇率的 as-of 查询
- tax: 累进预扣计算
- vesting: 归属引擎(休假暂停计时、部分达标、离职政策、黑窗期)
- settlement: 结算引擎(净股交付、卖股缴税、零股现金补偿)
- approvals: 人事/薪资/证券三岗位确认与退回留痕
- store: 仓储(锁、幂等键、台账、事件日志、.runtime 持久化)
- gateway: 证券过户与付款通道(可失败、可重试)
- service: EquityService 门面
- reports: 员工对账单与管理员追踪视图
"""

from .service import EquityService

__all__ = ["EquityService"]
