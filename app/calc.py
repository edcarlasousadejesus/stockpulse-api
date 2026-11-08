"""Regras de cálculo do StockPulse. Funções puras: sem banco, sem HTTP."""
from decimal import Decimal, ROUND_HALF_UP

D = Decimal
CENT = D("0.01")


def money(v) -> Decimal:
    return D(v).quantize(CENT, rounding=ROUND_HALF_UP)


def cmv(estoque_inicial, compras_liquidas, estoque_final) -> Decimal:
    """CMV = estoque inicial + compras líquidas - estoque final."""
    return money(D(estoque_inicial) + D(compras_liquidas) - D(estoque_final))


def cmv_pct(valor_cmv, faturamento) -> Decimal:
    if D(faturamento) <= 0:
        raise ValueError("faturamento deve ser maior que zero")
    return (D(valor_cmv) / D(faturamento) * 100).quantize(D("0.1"), rounding=ROUND_HALF_UP)


def margem_bruta(faturamento, valor_cmv) -> Decimal:
    """Margem bruta, não lucro líquido: ainda há salários, aluguel e impostos."""
    return money(D(faturamento) - D(valor_cmv))


def custo_medio_ponderado(saldo, custo_atual, qtd_compra, custo_unit) -> Decimal:
    saldo, qtd_compra = max(D(saldo), D(0)), D(qtd_compra)
    if qtd_compra <= 0:
        raise ValueError("quantidade da compra deve ser maior que zero")
    if saldo == 0:
        return D(custo_unit)
    return (saldo * D(custo_atual) + qtd_compra * D(custo_unit)) / (saldo + qtd_compra)


def custo_ficha(itens, outros_custos=0) -> Decimal:
    """itens: iterável de (quantidade, custo_unitario) na unidade de estoque."""
    return money(sum((D(q) * D(c) for q, c in itens), D(0)) + D(outros_custos))


def divergencia(cmv_real, custo_teorico) -> dict:
    """Sinal para investigar; não é prova de desperdício."""
    diff = money(D(cmv_real) - D(custo_teorico))
    pct = (diff / D(custo_teorico) * 100).quantize(D("0.1"), rounding=ROUND_HALF_UP) if D(custo_teorico) else None
    return {"diferenca": diff, "percentual": pct}
