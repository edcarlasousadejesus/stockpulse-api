from decimal import Decimal as D
import pytest
from fastapi.testclient import TestClient
from app.main import app
from conftest import EST_A, EST_B

c = TestClient(app)
GER = {"X-Estabelecimento": EST_A, "X-Perfil": "gerente", "X-Usuario": "ana"}
OPE = {**GER, "X-Perfil": "operador"}
OUTRO = {**GER, "X-Estabelecimento": EST_B}
DIA = "2026-10-14"


@pytest.fixture(autouse=True)
def _db(limpo):
    pass


def post(url, body, h=GER):
    return c.post(url, json=body, headers=h)


def montar():
    for n, u, custo in [("Carne", "kg", "32"), ("Queijo", "kg", "38"), ("Pão", "un", "1.20")]:
        assert post("/produtos", {"nome": n, "unidade": u, "custo": custo}).status_code == 201
    r = post("/fichas", {"nome": "Hambúrguer", "outros_custos": "0.86",
                         "itens": [{"produto": "Pão", "quantidade": "1"}, {"produto": "Carne", "quantidade": "0.15"}, {"produto": "Queijo", "quantidade": "0.03"}]})
    assert r.status_code == 201 and D(r.json()["custo"]) == D("8.00")


def comprar(prod, q, custo, h=GER):
    return post("/compras", {"fornecedor": "Boi Bom", "produto": prod, "quantidade": q, "custo_unit": custo, "data": DIA}, h)


def saldo(prod):
    itens = c.get("/estoque", headers=GER).json()["itens"]
    return D(next(i["saldo"] for i in itens if i["produto"] == prod))


def test_compra_atualiza_estoque_e_custo_medio():
    montar()
    comprar("Carne", "10", "32")
    r = comprar("Carne", "10", "42")
    assert r.status_code == 201 and D(r.json()["novo_custo_medio"]) == D("37")
    assert saldo("Carne") == D("20")


def test_venda_da_baixa_automatica_e_custo_teorico():
    montar()
    comprar("Carne", "20", "32"); comprar("Pão", "100", "1.20"); comprar("Queijo", "5", "38")
    r = post("/vendas", {"ficha": "Hambúrguer", "quantidade": "40", "preco_unit": "28", "data": DIA})
    j = r.json()
    assert r.status_code == 201 and D(j["custo_teorico"]) == D("320.00") and D(j["faturamento"]) == D("1120.00")
    assert j["estoque_negativo"] == []
    assert saldo("Carne") == D("14") and saldo("Pão") == D("60") and saldo("Queijo") == D("3.8")


def test_venda_sem_estoque_sinaliza_negativo():
    montar()
    j = post("/vendas", {"ficha": "Hambúrguer", "quantidade": "2", "preco_unit": "28", "data": DIA}).json()
    assert set(j["estoque_negativo"]) == {"Carne", "Pão", "Queijo"}


def test_perda_baixa_estoque_e_calcula_valor():
    montar(); comprar("Queijo", "5", "38")
    r = post("/perdas", {"produto": "Queijo", "quantidade": "1.5", "motivo": "vencimento", "data": DIA})
    assert r.status_code == 201 and D(r.json()["valor"]) == D("57.00")
    assert saldo("Queijo") == D("3.5")
    assert post("/perdas", {"produto": "Queijo", "quantidade": "9", "motivo": "quebra", "data": DIA}).status_code == 409


def test_contagem_fisica_ajusta_saldo_e_saida_excessiva_e_barrada():
    montar(); comprar("Carne", "10", "32")
    assert post("/estoque/movimentacoes", {"produto": "Carne", "tipo": "contagem", "quantidade": "8.5", "data": DIA}).status_code == 201
    assert saldo("Carne") == D("8.5")
    assert post("/estoque/movimentacoes", {"produto": "Carne", "tipo": "saida", "quantidade": "20", "data": DIA}).status_code == 409


def test_permissoes_do_operador():
    montar()
    assert post("/produtos", {"nome": "Bacon", "unidade": "kg", "custo": "40"}, OPE).status_code == 403
    assert post("/fichas", {"nome": "X", "itens": [{"produto": "Pão", "quantidade": "1"}]}, OPE).status_code == 403
    assert comprar("Carne", "5", "32", OPE).status_code == 201  # operador registra compras


def test_produto_inexistente_e_duplicado():
    montar()
    assert comprar("Alface", "1", "5").status_code == 404
    assert post("/produtos", {"nome": "Carne", "unidade": "kg", "custo": "1"}).status_code == 409
    assert post("/fichas", {"nome": "Y", "itens": [{"produto": "Alface", "quantidade": "1"}]}).status_code == 404


def test_isolamento_entre_estabelecimentos():
    montar(); comprar("Carne", "10", "32")
    assert c.get("/produtos", headers=OUTRO).json() == []
    assert c.get("/estoque", headers=OUTRO).json()["valor_total"] == "0.00"
    assert comprar("Carne", "1", "1", OUTRO).status_code == 404


def test_sugestao_preenche_e_fechamento_fecha():
    montar()
    comprar("Carne", "20", "32"); comprar("Pão", "100", "1.20"); comprar("Queijo", "5", "38")
    post("/vendas", {"ficha": "Hambúrguer", "quantidade": "40", "preco_unit": "28", "data": DIA})
    post("/perdas", {"produto": "Queijo", "quantidade": "1.5", "motivo": "vencimento", "data": DIA})
    s = c.get(f"/fechamentos/sugestao?data={DIA}", headers=GER).json()
    assert D(s["compras_liquidas"]) == D("640") + D("120") + D("190") and D(s["faturamento"]) == D("1120")
    assert s["estoque_inicial"] is None and D(s["custo_teorico"]) == D("320") and D(s["perdas"]) == D("57")
    corpo = {"data": DIA, "faturamento": s["faturamento"], "estoque_inicial": "0", "compras_liquidas": s["compras_liquidas"],
             "estoque_final": s["estoque_final"], "custo_teorico": s["custo_teorico"], "perdas": s["perdas"]}
    r = post("/fechamentos", corpo)
    assert r.status_code == 201 and D(r.json()["cmv"]) >= 0
    amanha = c.get("/fechamentos/sugestao?data=2026-10-15", headers=GER).json()
    assert D(amanha["estoque_inicial"]) == D(s["estoque_final"])
