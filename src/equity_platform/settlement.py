"""结算计算：净股交付、卖股缴税、全额交付，以及不足一股现金补偿与跨境税务折算。

确定性约定：
- 金额一律两位小数（ROUND_HALF_UP），股数交付向下取整；
- 抵税股数向上取整，保证税款足额覆盖，多抵部分以现金返还员工；
- 同一组输入永远得到同一个 SettlementQuote。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from .errors import SettlementInputError
from .models import SettlementMethod

CENT = Decimal("0.01")
ZERO = Decimal("0")


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _ceil_shares(amount: Decimal, price: Decimal) -> int:
    if amount <= 0:
        return 0
    return int((amount / price).to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class SettlementQuote:
    """一次结算的完整试算结果（员工对账与入账的唯一依据）。"""

    method: SettlementMethod
    gross_shares: Decimal              # 应归属股数（绩效调整后）
    price: Decimal                     # 结算价（计价货币）
    price_currency: str
    tax_currency: str
    fx_rate: Decimal                   # 1 计价货币 = fx_rate 计税货币
    withholding_rate: Decimal
    gross_value: Decimal               # 计税基础（计价货币）
    taxable_income_tax_ccy: Decimal    # 应税所得（计税货币）
    tax_due_tax_ccy: Decimal           # 应纳税额（计税货币）
    tax_due_price_ccy: Decimal         # 应纳税额（计价货币）
    shares_for_tax: int                # 净股代扣或卖出缴税的股数
    tax_cover_value: Decimal           # 抵税股数对应市值
    cash_residual_to_employee: Decimal # 多抵/多卖部分返还员工
    cash_tax_due_from_employee: Decimal  # 股份不足抵税或全额交付时员工应补缴现金
    net_shares_exact: Decimal          # 扣税后精确净股数
    delivered_shares: int              # 实际交付整股数
    fractional_shares: Decimal         # 不足一股部分
    cash_in_lieu: Decimal              # 不足一股现金补偿

    @property
    def total_cash_to_employee(self) -> Decimal:
        """员工现金净额（为负表示员工需补缴）。"""
        return q2(self.cash_residual_to_employee + self.cash_in_lieu - self.cash_tax_due_from_employee)


def compute_settlement(
    *,
    gross_shares: Decimal,
    price: Decimal,
    price_currency: str,
    tax_currency: str,
    fx_rate: Decimal | None,
    withholding_rate: Decimal,
    method: SettlementMethod,
) -> SettlementQuote:
    if gross_shares <= 0:
        raise SettlementInputError("结算股数必须为正数")
    if price <= 0:
        raise SettlementInputError("结算价格必须为正数")
    if withholding_rate < 0:
        raise SettlementInputError("预扣税率不能为负")
    fx = Decimal("1") if tax_currency == price_currency else fx_rate
    if fx is None or fx <= 0:
        raise SettlementInputError("缺少有效汇率")

    gross_value = q2(gross_shares * price)
    taxable_income = q2(gross_value * fx)
    tax_due_tax_ccy = q2(taxable_income * withholding_rate)
    tax_due_price_ccy = q2(tax_due_tax_ccy / fx)

    cash_tax_due = ZERO
    if method is SettlementMethod.GROSS:
        # 全额交付：不扣股，税款由员工以现金补缴（工资代扣）
        shares_for_tax = 0
        tax_cover_value = ZERO
        cash_residual = ZERO
        cash_tax_due = tax_due_price_ccy
    else:
        # 净股交付 / 卖股缴税：向上取整扣股，保证税款足额覆盖
        shares_for_tax = _ceil_shares(tax_due_price_ccy, price)
        if Decimal(shares_for_tax) > gross_shares:
            # 极端情形（税率过高）：全部股份抵税仍不足，差额员工补缴现金
            shares_for_tax = int(gross_shares.to_integral_value(rounding=ROUND_FLOOR))
            tax_cover_value = q2(Decimal(shares_for_tax) * price)
            cash_residual = ZERO
            cash_tax_due = q2(tax_due_price_ccy - tax_cover_value)
        else:
            tax_cover_value = q2(Decimal(shares_for_tax) * price)
            cash_residual = q2(tax_cover_value - tax_due_price_ccy)

    net_shares_exact = gross_shares - Decimal(shares_for_tax)
    delivered_shares = int(net_shares_exact.to_integral_value(rounding=ROUND_FLOOR))
    fractional_shares = net_shares_exact - Decimal(delivered_shares)
    cash_in_lieu = q2(fractional_shares * price)

    return SettlementQuote(
        method=method,
        gross_shares=gross_shares,
        price=price,
        price_currency=price_currency,
        tax_currency=tax_currency,
        fx_rate=fx,
        withholding_rate=withholding_rate,
        gross_value=gross_value,
        taxable_income_tax_ccy=taxable_income,
        tax_due_tax_ccy=tax_due_tax_ccy,
        tax_due_price_ccy=tax_due_price_ccy,
        shares_for_tax=shares_for_tax,
        tax_cover_value=tax_cover_value,
        cash_residual_to_employee=cash_residual,
        cash_tax_due_from_employee=cash_tax_due,
        net_shares_exact=net_shares_exact,
        delivered_shares=delivered_shares,
        fractional_shares=fractional_shares,
        cash_in_lieu=cash_in_lieu,
    )
