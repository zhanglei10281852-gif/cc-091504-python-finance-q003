"""精度与舍入约定,全平台统一,保证结果确定。"""

from decimal import Decimal

# 股数中间精度:归属数量保留 4 位小数,向下舍入避免超发
SHARE_QUANT = Decimal("0.0001")
# 金额精度:2 位小数,四舍五入(ROUND_HALF_UP)
MONEY_QUANT = Decimal("0.01")
# 比例精度(绩效达成率、归属比例):4 位小数
RATIO_QUANT = Decimal("0.0001")
# 高精度中间量(税的币种间换算)
WORK_QUANT = Decimal("0.00000001")

# 默认业务日期时区(与 reference/domain.json 一致)
DEFAULT_TIMEZONE = "Asia/Shanghai"
