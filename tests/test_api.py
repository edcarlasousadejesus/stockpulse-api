import pytest
from fastapi.testclient import TestClient
from app.main import app
from conftest import EST_A, EST_B

c = TestClient(app)
GER = {"X-Estabelecimento": EST_A, "X-Perfil": "gerente", "X-Usuario": "ana"}
OPE = {**GER, "X-Perfil": "operador"}
OUTRO = {**GER, "X-Estabelecimento": EST_B}
DIA = {"data": "2026-10-14", "faturamento": "5000", "estoque_inicial": "4000",
       "compras_liquidas": "1000", "estoque_final": "3500", "custo_teorico": "1440", "perdas": "120"}


@pytest.fixture(autouse=True)
def _db(limpo):
    pass


def test_fechar_e_apurar():
    r = c.post("/fechamentos", json=DIA, headers=GER)
    assert r.status_code == 201
    j = r.json()
    assert j["cmv"] == "1500.00" and j["cmv_pct"] == "30.0" and j["acima_da_meta"] is True
    assert j["divergencia"]["diferenca"] == "60.00"


def test_operador_nao_fecha_dia():
    assert c.post("/fechamentos", json=DIA, headers=OPE).status_code == 403


def test_dia_fechado_nao_duplica():
    c.post("/fechamentos", json=DIA, headers=GER)
    assert c.post("/fechamentos", json=DIA, headers=GER).status_code == 409


def test_reabrir_exige_motivo_e_audita():
    c.post("/fechamentos", json=DIA, headers=GER)
    assert c.post("/fechamentos/2026-10-14/reabrir", json={"motivo": "x"}, headers=GER).status_code == 422
    assert c.post("/fechamentos/2026-10-14/reabrir", json={"motivo": "Contagem corrigida"}, headers=GER).status_code == 200
    log = c.get("/auditoria", headers=GER).json()
    assert log[-1]["motivo"] == "Contagem corrigida" and log[-1]["usuario"] == "ana"
    assert c.post("/fechamentos", json=DIA, headers=GER).status_code == 201  # pode fechar de novo


def test_isolamento_entre_estabelecimentos():
    c.post("/fechamentos", json=DIA, headers=GER)
    assert c.get("/fechamentos", headers=OUTRO).json() == []
    assert c.post("/fechamentos/2026-10-14/reabrir", json={"motivo": "tentativa indevida"}, headers=OUTRO).status_code == 404


def test_validacao_de_entrada():
    assert c.post("/cmv/calcular", json={**DIA, "faturamento": "0"}, headers=GER).status_code == 422
    assert c.post("/cmv/calcular", json=DIA).status_code == 422  # sem cabeçalhos


def test_resumo():
    c.post("/fechamentos", json=DIA, headers=GER)
    j = c.get("/relatorios/resumo", headers=GER).json()
    assert j["dias"] == 1 and j["cmv_medio_pct"] == "30.0" and j["pior_dia"]["cmv_pct"] == "30.0"
