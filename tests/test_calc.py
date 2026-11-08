from decimal import Decimal as D
import pytest
from app import calc


def test_cmv_do_exemplo():
    assert calc.cmv(4000, 1000, 3500) == D("1500.00")


def test_percentual_e_margem():
    assert calc.cmv_pct(1500, 5000) == D("30.0")
    assert calc.margem_bruta(5000, 1500) == D("3500.00")


def test_faturamento_zero_e_rejeitado():
    with pytest.raises(ValueError):
        calc.cmv_pct(100, 0)


def test_ficha_tecnica_hamburguer():
    itens = [(1, "1.20"), ("0.15", "32"), ("0.03", "38")]
    assert calc.custo_ficha(itens, "0.86") == D("8.00")


def test_custo_medio_ponderado():
    assert calc.custo_medio_ponderado(10, 30, 10, 40) == D("35")
    assert calc.custo_medio_ponderado(0, 30, 10, 40) == D("40")
    assert calc.custo_medio_ponderado(-5, 30, 10, 40) == D("40")  # saldo negativo não pesa
    with pytest.raises(ValueError):
        calc.custo_medio_ponderado(10, 30, 0, 40)


def test_divergencia_e_sinal_nao_prova():
    d = calc.divergencia(1500, 1440)
    assert d["diferenca"] == D("60.00") and d["percentual"] == D("4.2")
