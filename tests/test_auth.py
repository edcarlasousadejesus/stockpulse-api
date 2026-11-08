import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from conftest import EST_A, U_GER, U_OPE, U_B, U_SEM
from app import auth
from app.main import app

c = TestClient(app)
TOKENS = {"tok-ana": U_GER, "tok-beto": U_OPE, "tok-caio": U_B, "tok-sem": U_SEM}


@pytest.fixture(autouse=True)
def _modo_token(limpo, monkeypatch):
    monkeypatch.delenv("STOCKPULSE_AUTH", raising=False)

    def falso(token):
        if token not in TOKENS:
            raise HTTPException(401, "token inválido ou expirado")
        return {"id": TOKENS[token]}
    monkeypatch.setattr(auth, "usuario_do_token", falso)

    def entrar(email, senha):
        if senha != "certa123":
            raise HTTPException(401, "e-mail ou senha incorretos")
        return {"access_token": "tok-ana", "expires_in": 3600}
    monkeypatch.setattr(auth, "entrar", entrar)


def h(t):
    return {"Authorization": f"Bearer {t}"}


def test_sem_token_e_token_invalido():
    assert c.get("/produtos").status_code == 401
    assert c.get("/produtos", headers=h("falso")).status_code == 401


def test_cabecalhos_de_teste_nao_valem_em_modo_normal():
    assert c.get("/produtos", headers={"X-Estabelecimento": EST_A, "X-Perfil": "administrador"}).status_code == 401


def test_token_valido_resolve_usuario_perfil_e_estabelecimento():
    j = c.get("/eu", headers=h("tok-ana")).json()
    assert j == {"usuario": "Ana", "perfil": "gerente", "estabelecimento_id": EST_A}


def test_perfil_vem_do_banco_e_operador_e_barrado():
    corpo = {"nome": "Carne", "unidade": "kg", "custo": "32"}
    assert c.post("/produtos", json=corpo, headers=h("tok-beto")).status_code == 403
    assert c.post("/produtos", json=corpo, headers=h("tok-ana")).status_code == 201


def test_usuario_sem_vinculo_nao_acessa_nada():
    r = c.get("/produtos", headers=h("tok-sem"))
    assert r.status_code == 403


def test_isolamento_por_token():
    c.post("/produtos", json={"nome": "Carne", "unidade": "kg", "custo": "32"}, headers=h("tok-ana"))
    assert len(c.get("/produtos", headers=h("tok-ana")).json()) == 1
    assert c.get("/produtos", headers=h("tok-caio")).json() == []


def test_login_devolve_token_e_dados_do_usuario():
    r = c.post("/auth/entrar", json={"email": "ana@exemplo.com", "senha": "certa123"})
    j = r.json()
    assert r.status_code == 200 and j["access_token"] == "tok-ana" and j["usuario"]["perfil"] == "gerente"
    assert c.post("/auth/entrar", json={"email": "ana@exemplo.com", "senha": "errada"}).status_code == 401


def test_auditoria_registra_quem_fez():
    c.post("/produtos", json={"nome": "Carne", "unidade": "kg", "custo": "32"}, headers=h("tok-ana"))
    log = c.get("/auditoria", headers=h("tok-ana")).json()
    assert log[-1]["usuario"] == "Ana" and log[-1]["perfil"] == "gerente"
