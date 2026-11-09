import json

import httpx
import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from conftest import EST_A, EST_B, U_B, U_GER, U_OPE, URL
from app import auth, contas, core, main, senha
from app.limites import Janela
from app.main import app

c = TestClient(app)
TOKENS = {"tok-ana": U_GER, "tok-beto": U_OPE, "tok-caio": U_B}
CAD = {"nome": "Maria da Silva", "email": "Maria@Exemplo.com", "senha": "Tr0cad0-Cafe-77", "confirmar_senha": "Tr0cad0-Cafe-77",
       "nome_negocio": "Açaí da Maria", "tipo_negocio": "acai"}
CHAMADAS: list = []


class R:
    def __init__(self, status, corpo=None):
        self.status_code, self._c = status, corpo

    def json(self):
        if self._c is None:
            raise ValueError
        return self._c


def db(q, p=()):
    with psycopg.connect(URL, autocommit=True) as x:
        cur = x.execute(q, p or None)
        return cur.fetchall() if cur.description else None


def h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture(autouse=True)
def _modo(limpo, monkeypatch):
    CHAMADAS.clear()
    monkeypatch.delenv("STOCKPULSE_AUTH", raising=False)

    def quem(token):
        if token == "tok-rec":
            return {"id": U_GER, "email": "ana@x.com", "user_metadata": {"nome": "Ana Souza"}}
        if token not in TOKENS:
            raise HTTPException(401, "token inválido ou expirado")
        return {"id": TOKENS[token]}
    monkeypatch.setattr(auth, "usuario_do_token", quem)
    for nome in ("cadastrar", "recuperar", "redefinir", "convidar", "sair"):
        monkeypatch.setattr(auth, nome, lambda *a, _n=nome, **k: CHAMADAS.append((_n, a, k)))


def login_ok(monkeypatch, token="tok-ana"):
    monkeypatch.setattr(auth, "entrar", lambda e, s: {"access_token": token, "refresh_token": "ref-" + token, "expires_in": 3600})


def login_falha(monkeypatch):
    def f(e, s):
        raise HTTPException(401, "e-mail ou senha incorretos")
    monkeypatch.setattr(auth, "entrar", f)


# ---------- política de senha e limitador ----------
@pytest.mark.parametrize("s,trecho", [("Senha123", "10 caracteres"), ("senhaforte123", "maiúscula"), ("SENHAFORTE123", "minúscula"), ("SenhaForteAbc", "número"),
                                      ("Aa1" * 30, "72"), ("Maria.Silva9A", "e-mail"), ("Joaquim#2024ab", "nome")])
def test_senha_fraca(s, trecho):
    assert any(trecho in p for p in senha.problemas(s, "maria.silva@x.com", "Joaquim Souza"))


def test_senha_forte_e_comuns():
    assert senha.problemas("Tr0cad0-Cafe-77", "a@b.com", "Maria Silva") == []
    assert any("comum" in p for p in senha.problemas("Brasil123", ""))


def test_limitador_bloqueia_e_informa_quando_liberar():
    j = Janela(2, 60)
    j.registrar("k"); j.registrar("k")
    with pytest.raises(HTTPException) as e:
        j.checar("k")
    assert e.value.status_code == 429 and 1 <= int(e.value.headers["Retry-After"]) <= 60
    j.zerar("k"); j.checar("k")


# ---------- cadastro ----------
def test_cadastro_envia_so_o_necessario_e_ignora_privilegios():
    r = c.post("/auth/cadastro", json={**CAD, "perfil": "administrador", "estabelecimento_id": EST_A, "app_metadata": {"perfil": "administrador"}})
    nome, args, _ = CHAMADAS[0]
    assert r.status_code == 202 and nome == "cadastrar" and args[0] == "maria@exemplo.com"
    assert args[2] == {"nome": "Maria da Silva", "nome_negocio": "Açaí da Maria", "tipo_negocio": "acai"}  # nenhum perfil nem estabelecimento


@pytest.mark.parametrize("mut", [
    {"senha": "Senha123", "confirmar_senha": "Senha123"}, {"confirmar_senha": "Outra-Senha-99"}, {"email": "maria@exemplo"}, {"email": "sem-arroba.com"},
    {"nome": "Maria"}, {"nome": "M S"}, {"tipo_negocio": "padaria"}, {"senha": "A" * 129, "confirmar_senha": "A" * 129}, {"nome": ""}])
def test_cadastro_invalido_nao_chega_ao_supabase(mut):
    assert c.post("/auth/cadastro", json={**CAD, **mut}).status_code == 422 and CHAMADAS == []


def test_cadastro_nao_revela_se_o_email_ja_existe_e_tem_limite():
    corpos = {c.post("/auth/cadastro", json=CAD).text for _ in range(2)}  # a 2ª tentativa seria "e-mail já cadastrado"
    assert len(corpos) == 1 and "confirmação" in corpos.pop()
    assert [c.post("/auth/cadastro", json=CAD).status_code for _ in range(4)] == [202, 202, 202, 429]


# ---------- login ----------
def test_login_devolve_sessao_com_refresh_token_e_perfil(monkeypatch):
    login_ok(monkeypatch)
    j = c.post("/auth/entrar", json={"email": "ANA@x.com ", "senha": "x"}).json()
    assert j["access_token"] == "tok-ana" and j["refresh_token"] == "ref-tok-ana" and j["usuario"]["perfil"] == "gerente" and j["usuario"]["estabelecimento_id"] == EST_A


def test_erro_de_login_e_igual_para_email_inexistente_e_senha_errada(monkeypatch):
    login_falha(monkeypatch)
    a = c.post("/auth/entrar", json={"email": "existe@x.com", "senha": "errada"})
    b = c.post("/auth/entrar", json={"email": "nao-existe@x.com", "senha": "errada"})
    assert a.status_code == b.status_code == 401 and a.text == b.text


def test_bloqueio_apos_tentativas_e_sucesso_zera(monkeypatch):
    login_falha(monkeypatch)
    corpo = {"email": "ana@x.com", "senha": "errada"}
    assert [c.post("/auth/entrar", json=corpo).status_code for _ in range(5)] == [401] * 5
    r = c.post("/auth/entrar", json=corpo)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    login_ok(monkeypatch)
    assert c.post("/auth/entrar", json=corpo).status_code == 429  # mesmo com a senha certa, enquanto bloqueado
    assert c.post("/auth/entrar", json={"email": "beto@x.com", "senha": "x"}).status_code in (200, 403)  # outro e-mail não é afetado


def test_sucesso_zera_o_contador(monkeypatch):
    corpo = {"email": "ana@x.com", "senha": "x"}
    login_falha(monkeypatch)
    for _ in range(4):
        c.post("/auth/entrar", json=corpo)
    login_ok(monkeypatch)
    assert c.post("/auth/entrar", json=corpo).status_code == 200
    login_falha(monkeypatch)
    assert [c.post("/auth/entrar", json=corpo).status_code for _ in range(4)] == [401] * 4


@pytest.mark.parametrize("corpo", [{"email": "x", "senha": "a"}, {"email": "a@b.com", "senha": ""}, {"email": "a@b.com", "senha": "x" * 129}, {"email": "a@b.com"}])
def test_login_valida_entrada(corpo):
    assert c.post("/auth/entrar", json=corpo).status_code == 422


def test_conta_sem_vinculo_nao_entra(monkeypatch):
    db("delete from usuarios where id = %s", (U_OPE,))
    login_ok(monkeypatch, "tok-beto")
    assert c.post("/auth/entrar", json={"email": "beto@x.com", "senha": "x"}).status_code == 403


# ---------- sessão ----------
def test_renovar_restaura_a_sessao(monkeypatch):
    monkeypatch.setattr(auth, "renovar", lambda rt: {"access_token": "tok-beto", "refresh_token": "novo-refresh-123", "expires_in": 3600})
    j = c.post("/auth/renovar", json={"refresh_token": "ref-antigo-123456"}).json()
    assert j["usuario"]["perfil"] == "operador" and j["refresh_token"] == "novo-refresh-123"

    def expirou(rt):
        raise HTTPException(401, "sessão expirada: entre novamente")
    monkeypatch.setattr(auth, "renovar", expirou)
    assert c.post("/auth/renovar", json={"refresh_token": "ref-antigo-123456"}).status_code == 401
    assert c.post("/auth/renovar", json={"refresh_token": "curto"}).status_code == 422


def test_sair_encerra_a_sessao_no_supabase():
    assert c.post("/auth/sair", headers=h("tok-ana")).status_code == 200 and CHAMADAS[0][0] == "sair" and CHAMADAS[0][1] == ("tok-ana",)
    assert c.post("/auth/sair").status_code == 200  # sem token também responde ok


@pytest.mark.parametrize("rota", ["/produtos", "/pedidos", "/estoque", "/clientes", "/estabelecimento", "/eu", "/equipe"])
def test_rotas_internas_exigem_login(rota):
    assert c.get(rota).status_code == 401
    assert c.get(rota, headers=h("falso")).status_code == 401


# ---------- recuperação de senha ----------
def test_recuperar_nunca_revela_se_o_email_existe_e_limita_envios():
    a, b = c.post("/auth/recuperar", json={"email": "existe@x.com"}), c.post("/auth/recuperar", json={"email": "nao-existe@x.com"})
    assert a.status_code == b.status_code == 202 and a.text == b.text
    for _ in range(3):  # a 4ª requisição do mesmo e-mail não envia nada, mas responde igual
        assert c.post("/auth/recuperar", json={"email": "existe@x.com"}).status_code == 202
    assert [x[1][0] for x in CHAMADAS if x[0] == "recuperar"].count("existe@x.com") == 3


def test_recuperar_limite_por_ip_e_email_invalido():
    assert c.post("/auth/recuperar", json={"email": "invalido"}).status_code == 422
    assert [c.post("/auth/recuperar", json={"email": f"u{i}@x.com"}).status_code for i in range(6)] == [202] * 5 + [429]


def test_redefinir_senha():
    nova = {"nova_senha": "Tr0cad0-Cafe-77", "confirmar_senha": "Tr0cad0-Cafe-77"}
    assert c.post("/auth/redefinir-senha", json=nova).status_code == 401
    assert c.post("/auth/redefinir-senha", json=nova, headers=h("falso")).status_code == 401
    assert c.post("/auth/redefinir-senha", json={"nova_senha": "fraca", "confirmar_senha": "fraca"}, headers=h("tok-rec")).status_code == 422
    assert c.post("/auth/redefinir-senha", json={**nova, "confirmar_senha": "outra"}, headers=h("tok-rec")).status_code == 422
    assert c.post("/auth/redefinir-senha", json={"nova_senha": "Ana-Souza-2026x", "confirmar_senha": "Ana-Souza-2026x"}, headers=h("tok-rec")).status_code == 422  # usa o nome
    assert CHAMADAS == []
    assert c.post("/auth/redefinir-senha", json=nova, headers=h("tok-rec")).status_code == 200
    assert CHAMADAS[0][0] == "redefinir" and CHAMADAS[0][1] == ("tok-rec", "Tr0cad0-Cafe-77")


# ---------- equipe ----------
def promover_ana():
    db("update usuarios set perfil = 'administrador' where id = %s", (U_GER,))


def test_equipe_so_administrador():
    assert c.get("/equipe", headers=h("tok-ana")).status_code == 403  # gerente
    assert c.get("/equipe", headers=h("tok-beto")).status_code == 403  # operador
    promover_ana()
    j = c.get("/equipe", headers=h("tok-ana")).json()
    assert {(x["nome"], x["email"], x["perfil"]) for x in j} == {("Ana", "ana@x.com", "administrador"), ("Beto", "beto@x.com", "operador")}


def test_convite_vai_para_o_proprio_estabelecimento_com_o_perfil_escolhido_pelo_servidor():
    promover_ana()
    r = c.post("/equipe/convites", json={"nome": "Davi Costa", "email": "Davi@X.com", "perfil": "gerente", "estabelecimento_id": EST_B}, headers=h("tok-ana"))
    assert r.status_code == 202 and CHAMADAS[0] == ("convidar", ("davi@x.com", "Davi Costa", EST_A, "gerente"), {})  # ignora o estabelecimento enviado
    assert c.post("/equipe/convites", json={"nome": "X Y", "email": "a@b.com", "perfil": "root"}, headers=h("tok-ana")).status_code == 422
    assert c.post("/equipe/convites", json={"nome": "X Y", "email": "a@b.com"}, headers=h("tok-beto")).status_code == 403
    assert db("select count(*) from auditoria where acao like 'convidou%'")[0][0] == 1


def test_mudar_perfil_com_protecoes():
    promover_ana()
    assert c.put(f"/equipe/{U_OPE}/perfil", json={"perfil": "gerente"}, headers=h("tok-ana")).status_code == 200
    assert db("select perfil::text from usuarios where id = %s", (U_OPE,))[0][0] == "gerente"
    assert c.put(f"/equipe/{U_B}/perfil", json={"perfil": "operador"}, headers=h("tok-ana")).status_code == 404  # outro estabelecimento
    assert c.put("/equipe/nao-e-uuid/perfil", json={"perfil": "operador"}, headers=h("tok-ana")).status_code == 404
    assert c.put(f"/equipe/{U_GER}/perfil", json={"perfil": "gerente"}, headers=h("tok-ana")).status_code == 409  # último administrador
    c.put(f"/equipe/{U_OPE}/perfil", json={"perfil": "administrador"}, headers=h("tok-ana"))
    assert c.put(f"/equipe/{U_GER}/perfil", json={"perfil": "gerente"}, headers=h("tok-ana")).status_code == 200


def test_remover_acesso_com_protecoes():
    promover_ana()
    assert c.delete(f"/equipe/{U_GER}", headers=h("tok-ana")).status_code == 409  # a si mesma
    assert c.delete(f"/equipe/{U_B}", headers=h("tok-ana")).status_code == 404
    assert c.get("/produtos", headers=h("tok-beto")).status_code == 200
    assert c.delete(f"/equipe/{U_OPE}", headers=h("tok-ana")).status_code == 200
    assert c.get("/produtos", headers=h("tok-beto")).status_code == 403  # sem vínculo, sem dados


# ---------- erro 500 no login: diagnóstico e mensagens claras ----------
def test_banco_sem_configuracao_ou_fora_do_ar_vira_503_claro(monkeypatch):
    def sem_url():
        raise RuntimeError("Defina a variável de ambiente DATABASE_URL")

    def fora():
        raise psycopg.OperationalError("connection refused")
    monkeypatch.setattr(core, "pool", sem_url)
    r = c.post("/auth/entrar", json={"email": "ana@x.com", "senha": "x"})
    assert r.status_code == 503 and "DATABASE_URL" in r.json()["detail"]
    monkeypatch.setattr(core, "pool", fora)
    r = c.post("/auth/entrar", json={"email": "ana@x.com", "senha": "x"})
    assert r.status_code == 503 and "indisponível" in r.json()["detail"] and "refused" not in r.text


def test_saude_nao_expoe_segredos(monkeypatch):
    monkeypatch.setattr(auth, "diagnostico", lambda: {"supabase": "ok", "confirmacao_de_email": True})
    r = c.get("/saude")
    assert r.status_code == 200 and r.json() == {"api": "ok", "banco": "ok", "supabase": "ok", "confirmacao_de_email": True, "producao": False}
    monkeypatch.setattr(main, "pool", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    r = c.get("/saude")
    assert r.status_code == 503 and r.json()["banco"] == "sem_configuracao"
    monkeypatch.setattr(auth, "diagnostico", lambda: {"supabase": "chave_invalida", "confirmacao_de_email": None})
    assert c.get("/saude").status_code == 503


# ---------- chamadas ao Supabase Auth (httpx simulado) ----------
@pytest.fixture
def sb(monkeypatch):
    for n in ("cadastrar", "recuperar", "redefinir", "convidar", "sair"):
        monkeypatch.undo() if False else None
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("AUTH_REDIRECT_URL", raising=False)
    cap = {"chamadas": [], "resp": R(200, {})}

    def req(metodo, url, headers=None, json=None, timeout=None):
        cap["chamadas"].append((metodo, url, headers, json))
        if isinstance(cap["resp"], Exception):
            raise cap["resp"]
        return cap["resp"]
    monkeypatch.setattr(httpx, "request", req)
    return cap


def reais(monkeypatch):
    import importlib
    importlib.reload(auth)


def test_supabase_cadastrar(sb, monkeypatch):
    reais(monkeypatch)
    sb["resp"] = R(200, {"id": "x"})
    monkeypatch.setenv("AUTH_REDIRECT_URL", "https://meusite.com/login")
    auth.cadastrar("a@b.com", "Senha-Forte-1", {"nome": "A B"}, "cap-token")
    m, url, hd, corpo = sb["chamadas"][0]
    assert m == "POST" and url.endswith("/auth/v1/signup?redirect_to=https://meusite.com/login") and hd["apikey"] == "anon"
    assert corpo == {"email": "a@b.com", "password": "Senha-Forte-1", "data": {"nome": "A B"}, "gotrue_meta_security": {"captcha_token": "cap-token"}}
    sb["resp"] = R(422, {"error_code": "user_already_exists", "msg": "User already registered"})
    auth.cadastrar("a@b.com", "Senha-Forte-1", {})  # não pode lançar: não revela que o e-mail existe
    for status, corpo_r, esperado in [(422, {"error_code": "weak_password"}, 422), (400, {"error_code": "email_address_invalid"}, 422), (429, {}, 429),
                                      (400, {"error_code": "captcha_failed"}, 400), (500, {}, 503)]:
        sb["resp"] = R(status, corpo_r)
        with pytest.raises(HTTPException) as e:
            auth.cadastrar("a@b.com", "Senha-Forte-1", {})
        assert e.value.status_code == esperado


def test_supabase_recuperar_renovar_sair_redefinir(sb, monkeypatch):
    reais(monkeypatch)
    for st in (200, 400, 404, 422):
        sb["resp"] = R(st, {})
        auth.recuperar("a@b.com")  # nunca revela se o e-mail existe
    for st, esperado in [(429, 429), (500, 503)]:
        sb["resp"] = R(st, {})
        with pytest.raises(HTTPException) as e:
            auth.recuperar("a@b.com")
        assert e.value.status_code == esperado
    sb["resp"] = R(200, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    assert auth.renovar("r0") == {"access_token": "a", "refresh_token": "r", "expires_in": 3600} and "grant_type=refresh_token" in sb["chamadas"][-1][1]
    for st, esperado in [(400, 401), (401, 401), (503, 503)]:
        sb["resp"] = R(st, {})
        with pytest.raises(HTTPException) as e:
            auth.renovar("r0")
        assert e.value.status_code == esperado
    sb["resp"] = httpx.ConnectError("x")
    auth.sair("tok")  # falha no logout remoto não pode quebrar o logout local
    sb["resp"] = R(200, {})
    auth.redefinir("tok", "Nova-Senha-123")
    assert sb["chamadas"][-1][0] == "PUT" and sb["chamadas"][-1][2]["Authorization"] == "Bearer tok" and sb["chamadas"][-1][3] == {"password": "Nova-Senha-123"}
    for st, cr, esperado in [(422, {"error_code": "weak_password"}, 422), (422, {"error_code": "same_password"}, 422), (401, {}, 401), (500, {}, 503)]:
        sb["resp"] = R(st, cr)
        with pytest.raises(HTTPException) as e:
            auth.redefinir("tok", "x")
        assert e.value.status_code == esperado


def test_supabase_convidar_usa_a_chave_de_administrador_so_no_servidor(sb, monkeypatch):
    reais(monkeypatch)
    with pytest.raises(HTTPException) as e:
        auth.convidar("a@b.com", "A B", EST_A, "operador")
    assert e.value.status_code == 503 and sb["chamadas"] == []
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-secreta")
    auth.convidar("a@b.com", "A B", EST_A, "operador")
    (m1, u1, h1, c1), (m2, u2, h2, c2) = sb["chamadas"]
    assert u1.endswith("/auth/v1/admin/users") and h1["Authorization"] == "Bearer svc-secreta" and c1["app_metadata"] == {"estabelecimento_id": EST_A, "perfil": "operador", "nome": "A B"}
    assert u2.endswith("/auth/v1/recover") and "svc-secreta" not in json.dumps(h2) and c2 == {"email": "a@b.com"}
    sb["resp"] = R(422, {"error_code": "email_exists"})
    with pytest.raises(HTTPException) as e:
        auth.convidar("a@b.com", "A B", EST_A, "operador")
    assert e.value.status_code == 409 and "e-mail" in e.value.detail


def test_supabase_diagnostico(monkeypatch):
    reais(monkeypatch)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    assert auth.diagnostico()["supabase"] == "nao_configurado"
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co"); monkeypatch.setenv("SUPABASE_ANON_KEY", "k")
    for resposta, esperado in [(R(200, {"mailer_autoconfirm": False}), ("ok", True)), (R(200, {"mailer_autoconfirm": True}), ("ok", False)), (R(200, {}), ("ok", None)),
                               (R(401, {}), ("chave_invalida", None)), (R(500, {}), ("erro", None)), (httpx.ConnectError("x"), ("inacessivel", None))]:
        def get(*a, _r=resposta, **k):
            if isinstance(_r, Exception):
                raise _r
            return _r
        monkeypatch.setattr(httpx, "get", get)
        d = auth.diagnostico()
        assert (d["supabase"], d["confirmacao_de_email"]) == esperado
