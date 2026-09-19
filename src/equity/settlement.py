"""结算引擎(纯函数)。

给定应归属股数、归属日市价、汇率与税务身份,计算确定性的交付构成:
- 整股交付,不足一股部分按市价折算现金补偿;
- 预扣税按税务身份累进计算,币种不同时用同一汇率快照双向换算;
- net_share:扣股抵税,多扣部分现金退还;
- sell_to_cover:卖股缴税,剩余现金返还员工;
- gross:不扣股,税额转工资代扣。
所有中间量进入 SettlementBreakdown,员工可逐项核对。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .codec import register
from .constants import WORK_QUANT
from .enums import SettlementMode
from .errors import ValidationError
from .models import TaxBracket
from .tax import money, progressive_tax

WHOLE = Decimal("1")


@register
@dataclass(frozen=True)
class SettlementBreakdown:
    """一次结算的完整构成,全部确定性可复算。"""

    mode: SettlementMode
    gross_shares: Decimal            # 应归属股数(含零股)
    whole_shares: Decimal            # 整股数
    fractional_shares: Decimal       # 不足一股部分
    price: Decimal                   # 归属日市价(股票币种)
    price_currency: str
    price_at: datetime
    fx_rate: Decimal | None          # 1 股票币种 = fx_rate 税务币种;同币种为 None
    fx_at: datetime | None
    tax_currency: str
    taxable_income_stock_ccy: Decimal
    taxable_income_tax_ccy: Decimal
    tax_due_tax_ccy: Decimal         # 预扣税(税务币种)
    tax_due_stock_ccy: Decimal       # 预扣税(股票币种,高精度)
    shares_for_tax: Decimal          # 扣股/卖股数量
    net_shares: Decimal              # 最终到账股数
    fractional_cash_tax_ccy: Decimal   # 零股现金补偿
    sell_residual_tax_ccy: Decimal     # 卖股缴税剩余返还
    withhold_refund_tax_ccy: Decimal   # 净股多扣现金退还
    employee_cash_due_tax_ccy: Decimal  # 税额超过交付价值时员工应补
    payroll_withholding_tax_ccy: Decimal  # gross 模式转工资代扣金额


def _ceil_whole(x: Decimal) -> Decimal:
    return x.quantize(WHOLE, rounding=ROUND_CEILING)


def compute_settlement(
    *,
    mode: SettlementMode,
    gross_shares: Decimal,
    price: Decimal,
    price_currency: str,
    price_at: datetime,
    fx_rate: Decimal | None,
    fx_at: datetime | None,
    tax_currency: str,
    brackets: tuple[TaxBracket, ...],
) -> SettlementBreakdown:
    if gross_shares <= 0:
        raise ValidationError("结算股数必须为正", code="invalid_shares")
    if price <= 0:
        raise ValidationError("市价必须为正", code="invalid_price")

    same_currency = tax_currency == price_currency
    if not same_currency and (fx_rate is None or fx_rate <= 0):
        raise ValidationError(
            f"税务币种 {tax_currency} 与股票币种 {price_currency} 不同,缺少有效汇率",
            code="fx_missing",
        )
    fx = Decimal("1") if same_currency else fx_rate

    whole = gross_shares.quantize(WHOLE, rounding=ROUND_FLOOR)
    fractional = gross_shares - whole

    taxable_stock = gross_shares * price
    taxable_tax = money(taxable_stock * fx)
    tax_due = money(progressive_tax(taxable_tax, brackets))
    tax_stock = (tax_due / fx).quantize(WORK_QUANT)

    shares_for_tax = Decimal("0")
    sell_residual = Decimal("0")
    withhold_refund = Decimal("0")
    employee_due = Decimal("0")
    payroll_withholding = Decimal("0")

    if mode == SettlementMode.GROSS:
        payroll_withholding = tax_due
    else:
        needed = _ceil_whole(tax_stock / price)
        covered_value_tax = money(needed * price * fx)
        if needed <= whole:
            shares_for_tax = needed
        else:
            # 税额超过整股价值:全部整股抵税,差额员工现金补缴
            shares_for_tax = whole
            employee_due = money(tax_due - money(whole * price * fx))
        if mode == SettlementMode.NET_SHARE:
            withhold_refund = money(max(Decimal("0"), covered_value_tax - tax_due))
            if shares_for_tax < needed:
                withhold_refund = Decimal("0")
        else:  # SELL_TO_COVER
            sell_residual = money(max(Decimal("0"), covered_value_tax - tax_due))
            if shares_for_tax < needed:
                sell_residual = Decimal("0")

    net_shares = whole - shares_for_tax
    fractional_cash = money(fractional * price * fx)

    return SettlementBreakdown(
        mode=mode,
        gross_shares=gross_shares,
        whole_shares=whole,
        fractional_shares=fractional,
        price=price,
        price_currency=price_currency,
        price_at=price_at,
        fx_rate=None if same_currency else fx,
        fx_at=None if same_currency else fx_at,
        tax_currency=tax_currency,
        taxable_income_stock_ccy=money(taxable_stock),
        taxable_income_tax_ccy=taxable_tax,
        tax_due_tax_ccy=tax_due,
        tax_due_stock_ccy=tax_stock,
        shares_for_tax=shares_for_tax,
        net_shares=net_shares,
        fractional_cash_tax_ccy=fractional_cash,
        sell_residual_tax_ccy=sell_residual,
        withhold_refund_tax_ccy=withhold_refund,
        employee_cash_due_tax_ccy=employee_due,
        payroll_withholding_tax_ccy=payroll_withholding,
    )
