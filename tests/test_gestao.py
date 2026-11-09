import importlib
import json
import uuid
from decimal import Decimal as D

import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import EST_A, EST_B, RAIZ, URL
from app import publico
from app.main import app
from app.modelos import MODELOS

c = TestClient(app)
GER = {"X-Estabelecimento": EST_A, "X-Perfil": "gerente", "X-Usuario": "ana"}
OPE = {**GER, "X-Perfil": "operador"}
GER_B = {**GER, "X-Estabelecimento": EST_B}
DIA = "2026-10-14"


@pytest.fixture(autouse=True)
def _db(limpo):
    publico._TENTATIVAS.clear()


def db(q, p=()):
    with psycopg.connect(URL, autocommit=True) as x:
        cur = x.execute(q, p)
        return cur.fetchall() if cur.description else None


def post(u, b, h=GER):
    return c.post(u, json=b, headers=h)


def put(u, b, h=GER):
    return c.put(u, json=b, headers=h)


def montar(h=GER):
    for n, u, custo in [("Carne", "kg", "32"), ("Queijo", "kg", "38"), ("Pão", "un", "1.20")]:
        assert post("/produtos", {"nome": n, "unidade": u, "custo": custo}, h).status_code == 201
    r = post("/fichas", {"nome": "Hambúrguer", "categoria": "Hambúrgueres", "preco_venda": "28", "outros_custos": "0.86",
                         "itens": [{"produto": "Pão", "quantidade": "1"}, {"produto": "Carne", "quantidade": "0.15"}, {"produto": "Queijo", "quantidade": "0.03"}]}, h)
    assert r.status_code == 201


def comprar(prod, q, custo, h=GER):
    return post("/compras", {"fornecedor": "F", "produto": prod, "quantidade": q, "custo_unit": custo, "data": DIA}, h)


def saldo(prod, h=GER):
    return D(next(i["saldo"] for i in c.get("/estoque", headers=h).json()["itens"] if i["produto"] == prod))


def pedido(nome="Ana", itens=None, tel=None, h=GER):
    return post("/pedidos", {"cliente_nome": nome, "cliente_telefone": tel, "itens": itens or [{"ficha": "Hambúrguer", "quantidade": "2"}]}, h)


# ---------- produtos de venda ----------
def test_ficha_com_preco_categoria_e_margem():
    montar()
    f = c.get("/fichas", headers=GER).json()[0]
    assert f["categoria"] == "Hambúrgueres" and D(f["custo"]) == D("8.00") and D(f["margem_unitaria"]) == D("20.00") and D(f["margem_pct"]) == D("71.4")


def test_promocao_mostra_impacto_e_valida():
    montar()
    j = put("/fichas/Hambúrguer/promocao", {"preco_promocional": "22.4"}).json()
    assert D(j["margem_pct"]) == D("64.3") and j["acima_da_meta"] is True and j["abaixo_do_custo"] is False  # CMV do item 35,7% > meta 28%
    assert put("/fichas/Hambúrguer/promocao", {"preco_promocional": "7"}).json()["abaixo_do_custo"] is True
    assert put("/fichas/Hambúrguer/promocao", {"preco_promocional": "30"}).status_code == 422
    assert put("/fichas/Hambúrguer/preco", {"preco_venda": "5"}).status_code == 409  # ficaria menor que a promoção (7)
    assert put("/fichas/Hambúrguer/promocao", {"preco_promocional": None}).json()["preco_promocional"] is None
    assert put("/fichas/Hambúrguer/preco", {"preco_venda": "30"}).status_code == 200
    assert put("/fichas/Hambúrguer/promocao", {"preco_promocional": "22.4"}, OPE).status_code == 403
    assert put("/fichas/Hambúrguer/disponibilidade", {"ativo": False}, OPE).status_code == 200  # operador marca esgotado


def test_venda_usa_preco_da_ficha_quando_omitido():
    montar()
    r = post("/vendas", {"ficha": "Hambúrguer", "quantidade": "2", "data": DIA})
    assert r.status_code == 201 and D(r.json()["faturamento"]) == D("56.00")
    montar_sem = post("/fichas", {"nome": "Sem preço", "itens": [{"produto": "Pão", "quantidade": "1"}]})
    assert post("/vendas", {"ficha": "Sem preço", "quantidade": "1", "data": DIA}).status_code == 422


# ---------- pedidos ----------
def test_preco_do_pedido_vem_do_servidor_e_numero_e_por_estabelecimento():
    montar()
    r = post("/pedidos", {"cliente_nome": "Ana", "itens": [{"ficha": "Hambúrguer", "quantidade": "2", "preco": "0.01"}], "total": "0.02"})
    assert r.status_code == 201 and D(r.json()["total"]) == D("56.00") and r.json()["numero"] == 1
    assert pedido().json()["numero"] == 2
    montar(GER_B)
    assert pedido(h=GER_B).json()["numero"] == 1
    put("/fichas/Hambúrguer/promocao", {"preco_promocional": "20"})
    assert D(pedido().json()["total"]) == D("40.00")  # preço promocional


def test_pedido_de_produto_indisponivel_e_recusado():
    montar()
    put("/fichas/Hambúrguer/disponibilidade", {"ativo": False})
    assert pedido().status_code == 422
    assert pedido(itens=[{"ficha": "Inexistente", "quantidade": "1"}]).status_code == 422
    put("/fichas/Hambúrguer/disponibilidade", {"ativo": True})
    assert pedido(itens=[{"ficha": "Hambúrguer", "quantidade": "100"}]).status_code == 422
    assert post("/pedidos", {"cliente_nome": "Ana", "cliente_telefone": "123", "itens": [{"ficha": "Hambúrguer", "quantidade": "1"}]}).status_code == 422


def test_fluxo_do_pedido_vira_venda_e_baixa_estoque():
    montar(); comprar("Carne", "20", "32"); comprar("Pão", "100", "1.20"); comprar("Queijo", "5", "38")
    n = pedido().json()["numero"]
    j = c.get("/pedidos", headers=GER).json()[0]
    assert j["status"] == "novo" and j["visto"] is False and j["itens"][0]["nome"] == "Hambúrguer"
    assert c.post(f"/pedidos/{n}/avancar", headers=GER).json()["status"] == "preparo"
    assert c.post(f"/pedidos/{n}/avancar", headers=GER).json()["status"] == "pronto"
    assert c.get("/vendas", headers=GER).json() == [] and saldo("Carne") == D("20")  # nada muda antes de concluir
    r = c.post(f"/pedidos/{n}/avancar", headers=GER)
    assert r.json()["status"] == "entregue" and r.json()["estoque_negativo"] == []
    assert saldo("Carne") == D("19.7") and saldo("Pão") == D("98")
    v = c.get("/vendas", headers=GER).json()
    assert len(v) == 1 and D(v[0]["faturamento"]) == D("56.00") and D(v[0]["custo_teorico"]) == D("16.00")
    assert c.post(f"/pedidos/{n}/avancar", headers=GER).status_code == 409
    assert db("select count(*) from vendas where pedido_id is not null")[0][0] == 1


def test_cancelar_pedido():
    montar(); comprar("Carne", "20", "32"); comprar("Pão", "100", "1.20"); comprar("Queijo", "5", "38")
    a, b = pedido().json()["numero"], pedido().json()["numero"]
    assert c.post(f"/pedidos/{a}/cancelar", headers=GER).json()["status"] == "cancelado"
    assert c.post(f"/pedidos/{a}/cancelar", headers=GER).status_code == 409
    assert c.post(f"/pedidos/{a}/avancar", headers=GER).status_code == 409
    for _ in range(3):
        c.post(f"/pedidos/{b}/avancar", headers=GER)
    assert c.post(f"/pedidos/{b}/cancelar", headers=GER).status_code == 409  # já entregue
    assert len(c.get("/vendas", headers=GER).json()) == 1
    assert c.post("/pedidos/999/avancar", headers=GER).status_code == 404


def test_visto_filtros_e_isolamento():
    montar(); n = pedido().json()["numero"]
    c.post(f"/pedidos/{n}/visto", headers=GER)
    assert c.get("/pedidos", headers=GER).json()[0]["visto"] is True
    assert len(c.get("/pedidos?status=novo", headers=GER).json()) == 1 and c.get("/pedidos?status=entregue", headers=GER).json() == []
    assert c.get("/pedidos", headers=GER_B).json() == []
    assert c.post(f"/pedidos/{n}/avancar", headers=GER_B).status_code == 404


def test_clientes_e_recuperacao():
    montar()
    pedido("Ana", tel="(83) 99999-0000"); pedido("Ana", tel="83999990000")
    pedido("Beto", [{"ficha": "Hambúrguer", "quantidade": "3"}], tel="83988887777")
    db("update pedidos set criado_em = now() - interval '20 days' where cliente_nome = 'Beto'")
    j = {x["nome"]: x for x in c.get("/clientes", headers=GER).json()}
    assert j["Ana"]["pedidos"] == 2 and j["Ana"]["situacao"] == "ativo" and j["Ana"]["telefone"] == "83999990000" and D(j["Ana"]["ticket_medio"]) == D("56.00")
    assert j["Beto"]["situacao"] == "recuperar" and j["Beto"]["dias_sem_pedir"] >= 19 and j["Beto"]["favorito"] == "Hambúrguer"
    assert list(j)[0] == "Beto" or c.get("/clientes", headers=GER).json()[0]["nome"] == "Beto"  # quem precisa de recuperação vem primeiro
    assert c.get("/clientes", headers=GER_B).json() == []


# ---------- negócio, marca e modelos ----------
def test_dados_do_negocio_e_marca():
    assert c.get("/estabelecimento", headers=GER).json()["slug"] == "loja-a"
    r = put("/estabelecimento", {"marca_nome": "Smash do Zé", "slogan": "O melhor smash", "whatsapp": "(83) 99888-7766", "cor_marca": "#FF8A00",
                                 "logo_url": "https://exemplo.com/logo.png", "cardapio_publico": True, "meta_cmv_pct": "31.5"})
    j = r.json()
    assert r.status_code == 200 and j["whatsapp"] == "83998887766" and j["cor_marca"] == "#FF8A00" and D(j["meta_cmv_pct"]) == D("31.5")
    assert put("/estabelecimento", {"slogan": None}).json()["slogan"] is None  # pode limpar campos opcionais


@pytest.mark.parametrize("corpo", [
    {"cor_marca": "#zzz"}, {"cor_marca": "red"}, {"cor_marca": "#fff;}</style><script>alert(1)</script>"},
    {"logo_url": "http://exemplo.com/a.png"}, {"logo_url": "javascript:alert(1)"}, {"logo_url": "https://x.com/a b.png"},
    {"whatsapp": "123"}, {"meta_cmv_pct": "0"}, {"meta_cmv_pct": "101"}, {"tipo_negocio": "padaria"}, {"nome": None}, {"cardapio_publico": None}, {}])
def test_dados_invalidos_sao_recusados(corpo):
    assert put("/estabelecimento", corpo).status_code == 422


def test_operador_nao_altera_o_negocio():
    assert put("/estabelecimento", {"slogan": "x"}, OPE).status_code == 403
    assert post("/estabelecimento/modelo", {"tipo": "bar"}, OPE).status_code == 403


def test_modelo_acai_custos_e_metas():
    r = post("/estabelecimento/modelo", {"tipo": "acai"})
    assert r.status_code == 201 and r.json()["produtos"] == 3
    f = {x["nome"]: x for x in c.get("/fichas", headers=GER).json()}
    assert D(f["Açaí 300 ml"]["custo"]) == D("8.88") and D(f["Açaí 300 ml"]["margem_pct"]) == D("44.5")
    e = c.get("/estabelecimento", headers=GER).json()
    assert e["tipo_negocio"] == "acai" and D(e["meta_cmv_pct"]) == D("30")
    assert post("/estabelecimento/modelo", {"tipo": "bar"}).status_code == 409  # nunca apaga dados
    assert c.get("/fichas", headers=GER_B).json() == []
    assert post("/estabelecimento/modelo", {"tipo": "padaria"}, GER_B).status_code == 422


@pytest.mark.parametrize("tipo", list(MODELOS))
def test_todos_os_modelos_aplicam(tipo):
    r = post("/estabelecimento/modelo", {"tipo": tipo})
    assert r.status_code == 201 and r.json()["produtos"] == len(MODELOS[tipo]["pr"]) and len(c.get("/fichas", headers=GER).json()) == len(MODELOS[tipo]["pr"])
    assert all(D(f["margem_pct"]) > 0 for f in c.get("/fichas", headers=GER).json())


# ---------- cardápio público ----------
def abrir_cardapio():
    montar()
    post("/fichas", {"nome": "Refrigerante", "categoria": "Bebidas", "preco_venda": "7", "itens": [{"produto": "Pão", "quantidade": "1"}]})
    post("/fichas", {"nome": "Esgotado", "categoria": "Bebidas", "preco_venda": "9", "itens": [{"produto": "Pão", "quantidade": "1"}]})
    post("/fichas", {"nome": "Sem preço", "itens": [{"produto": "Pão", "quantidade": "1"}]})
    put("/fichas/Esgotado/disponibilidade", {"ativo": False})
    put("/fichas/Hambúrguer/promocao", {"preco_promocional": "22.4"})
    put("/estabelecimento", {"marca_nome": "Smash do Zé", "whatsapp": "83998887766", "cor_marca": "#ff8a00", "cardapio_publico": True})


def test_cardapio_publico_desligado_ou_inexistente():
    montar()
    assert c.get("/publico/loja-a/cardapio").status_code == 404
    assert c.post("/publico/loja-a/pedidos", json={"cliente_nome": "X", "itens": [{"ficha": "Hambúrguer", "quantidade": "1"}]}).status_code == 404
    assert c.get("/publico/nao-existe/cardapio").status_code == 404


def test_cardapio_publico_so_mostra_o_que_o_cliente_deve_ver():
    abrir_cardapio()
    r = c.get("/publico/loja-a/cardapio")  # sem login
    j = r.json()
    assert r.status_code == 200 and j["nome"] == "Smash do Zé" and j["cor"] == "#ff8a00" and j["whatsapp"] == "83998887766"
    itens = {i["nome"]: i for cat in j["categorias"] for i in cat["itens"]}
    assert set(itens) == {"Hambúrguer", "Refrigerante"}  # sem o esgotado e sem o produto sem preço
    assert D(itens["Hambúrguer"]["preco"]) == D("22.40") and D(itens["Hambúrguer"]["preco_original"]) == D("28.00") and itens["Refrigerante"]["preco_original"] is None
    assert set(itens["Hambúrguer"]) == {"nome", "preco", "preco_original"}
    assert set(j) == {"nome", "slogan", "whatsapp", "cor", "logo_url", "categorias"}
    texto = r.text.lower()
    assert not any(p in texto for p in ("custo", "margem", "estoque", "cmv", "outros_custos", "\"id\""))
    assert c.get("/publico/loja-b/cardapio").status_code == 404  # outro estabelecimento continua fechado


def test_pedido_publico_chega_no_painel_com_preco_do_servidor():
    abrir_cardapio()
    r = c.post("/publico/loja-a/pedidos", json={"cliente_nome": "Carla", "cliente_telefone": "(83) 98888-0000", "observacao": "sem cebola",
                                                "itens": [{"ficha": "Hambúrguer", "quantidade": "2", "preco": "0.01"}, {"ficha": "Refrigerante", "quantidade": "1"}], "total": "0.01"})
    assert r.status_code == 201 and D(r.json()["total"]) == D("51.80") and r.json()["numero"] == 1
    p = c.get("/pedidos", headers=GER).json()[0]
    assert p["canal"] == "Cardápio" and p["status"] == "novo" and p["visto"] is False and p["cliente_telefone"] == "83988880000" and p["observacao"] == "sem cebola"
    assert c.get("/pedidos", headers=GER_B).json() == []
    assert db("select usuario_nome from auditoria order by id desc limit 1")[0][0] == "cardápio"


@pytest.mark.parametrize("corpo", [
    {"cliente_nome": "", "itens": [{"ficha": "Hambúrguer", "quantidade": "1"}]},
    {"cliente_nome": "X", "itens": []},
    {"cliente_nome": "X", "itens": [{"ficha": "Hambúrguer", "quantidade": "0"}]},
    {"cliente_nome": "X", "itens": [{"ficha": "Hambúrguer", "quantidade": "100"}]},
    {"cliente_nome": "X", "cliente_telefone": "12", "itens": [{"ficha": "Hambúrguer", "quantidade": "1"}]},
    {"cliente_nome": "X", "itens": [{"ficha": "Esgotado", "quantidade": "1"}]},
    {"cliente_nome": "X", "itens": [{"ficha": "Sem preço", "quantidade": "1"}]},
    {"cliente_nome": "X", "itens": [{"ficha": "Inexistente", "quantidade": "1"}]},
    {"cliente_nome": "X" * 81, "itens": [{"ficha": "Hambúrguer", "quantidade": "1"}]}])
def test_pedido_publico_invalido(corpo):
    abrir_cardapio()
    assert c.post("/publico/loja-a/pedidos", json=corpo).status_code == 422


def test_pedido_publico_nao_usa_produto_de_outro_estabelecimento():
    abrir_cardapio()
    put("/estabelecimento", {"cardapio_publico": True}, GER_B)
    assert c.post("/publico/loja-b/pedidos", json={"cliente_nome": "X", "itens": [{"ficha": "Hambúrguer", "quantidade": "1"}]}).status_code == 422


def test_limite_de_pedidos_publicos():
    abrir_cardapio()
    corpo = {"cliente_nome": "X", "itens": [{"ficha": "Refrigerante", "quantidade": "1"}]}
    assert [c.post("/publico/loja-a/pedidos", json=corpo).status_code for _ in range(10)] == [201] * 10
    assert c.post("/publico/loja-a/pedidos", json=corpo).status_code == 429


# ---------- banco: cadastro automático, RLS e migrações ----------
def novo_auth(meta=None, app_meta=None):
    uid = str(uuid.uuid4())
    db("insert into auth.users (id, email, raw_user_meta_data, raw_app_meta_data) values (%s, %s, %s::jsonb, %s::jsonb)",
       (uid, f"{uid[:6]}@x.com", json.dumps(meta or {}), json.dumps(app_meta or {})))
    return uid


def usuario(uid):
    return db("select u.nome, u.perfil::text, u.estabelecimento_id::text, e.nome, e.tipo_negocio, e.slug from usuarios u join estabelecimentos e on e.id = u.estabelecimento_id where u.id = %s", (uid,))[0]


def test_cadastro_cria_estabelecimento_e_administrador():
    nome, perfil, est, neg, tipo, slug = usuario(novo_auth({"nome_negocio": "Açaí da Maria", "tipo_negocio": "acai", "nome": "Maria"}))
    assert (nome, perfil, neg, tipo) == ("Maria", "administrador", "Açaí da Maria", "acai") and slug.startswith("acai-da-maria-") and est not in (EST_A, EST_B)


def test_cadastro_com_dados_invalidos_usa_padroes():
    _, _, _, neg, tipo, slug = usuario(novo_auth({"tipo_negocio": "xyz"}))
    assert neg == "Meu negócio" and tipo == "outro" and slug.startswith("meu-negocio-")


def test_usuario_nao_consegue_entrar_em_outro_estabelecimento_pelo_proprio_cadastro():
    # raw_user_meta_data é editável pelo usuário: nunca pode decidir o estabelecimento nem o perfil
    uid = novo_auth({"estabelecimento_id": EST_A, "perfil": "administrador", "nome_negocio": "Meu"})
    _, perfil, est, *_ = usuario(uid)
    assert est != EST_A and perfil == "administrador"
    assert db("select count(*) from usuarios where estabelecimento_id = %s", (EST_A,))[0][0] == 2  # só Ana e Beto


def test_convite_do_servidor_vincula_ao_estabelecimento():
    _, perfil, est, *_ = usuario(novo_auth(app_meta={"estabelecimento_id": EST_A, "perfil": "operador", "nome": "Davi"}))
    assert (perfil, est) == ("operador", EST_A)
    assert usuario(novo_auth(app_meta={"estabelecimento_id": EST_A, "perfil": "root"}))[1] == "operador"  # perfil inválido vira o mais restrito


def test_rls_ativo_nas_tabelas_novas_e_migracoes_idempotentes():
    assert all(r[0] for r in db("select relrowsecurity from pg_class where relname in ('pedidos', 'itens_pedido')"))
    for m in sorted((RAIZ / "migrations").glob("*.sql")):
        db(m.read_text(encoding="utf-8"))  # rodar de novo não pode falhar
    assert db("select count(*) from pg_trigger where tgname = 'ao_criar_usuario'")[0][0] == 1


# ---------- produção ----------
def recarregar():
    import app.main as m
    return importlib.reload(m).app


def test_documentacao_desligada_em_producao(monkeypatch):
    monkeypatch.setenv("STOCKPULSE_ENV", "producao")
    t = TestClient(recarregar())
    assert t.get("/docs").status_code == 404 and t.get("/openapi.json").status_code == 404 and t.get("/").json()["versao"] == "0.5.0"
    monkeypatch.delenv("STOCKPULSE_ENV"); recarregar()
    assert TestClient(recarregar()).get("/docs").status_code == 200


def test_cors_so_para_os_enderecos_permitidos(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://meusite.com.br")
    t = TestClient(recarregar())
    pedido_cors = lambda o: t.options("/publico/loja-a/cardapio", headers={"Origin": o, "Access-Control-Request-Method": "GET"})
    assert pedido_cors("https://meusite.com.br").headers.get("access-control-allow-origin") == "https://meusite.com.br"
    assert "access-control-allow-origin" not in pedido_cors("https://outro.com").headers
    monkeypatch.delenv("CORS_ORIGINS"); recarregar()
