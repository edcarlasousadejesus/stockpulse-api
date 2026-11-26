"""Testes do complemento: personalização IA, bloqueios, cross-sell, recuperação, entrega/motoboys e CRM."""
from decimal import Decimal
from fastapi.testclient import TestClient

from app import complemento, main, whatsapp
from app.core import Ctx, contexto, get_conn


class _FakeResult:
    def __init__(self, one=None, many=None):
        self._one = one
        self._many = many or []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class _FakeConn:
    def execute(self, sql, params=()):
        s = " ".join(sql.lower().split())
        if "from estabelecimentos" in s:
            return _FakeResult(one={
                "id": "11111111-1111-1111-1111-111111111111",
                "slug": "smash-do-ze",
                "nome": "Smash do Zé",
                "marca_nome": "Smash do Zé",
            })
        if "from fichas_tecnicas f" in s:
            return _FakeResult(many=[
                {"categoria": "Lanches", "nome": "Hambúrguer", "preco_venda": Decimal("28.00"), "preco_promocional": None},
                {"categoria": "Bebidas", "nome": "Refrigerante", "preco_venda": Decimal("7.00"), "preco_promocional": None},
            ])
        if "from pedidos where estabelecimento_id = %s and numero = %s" in s:
            return _FakeResult(one={"cliente_nome": "Carla", "cliente_telefone": "83988880000", "total": Decimal("35.00"), "pagamento": "Pix", "observacao": "Rua A, 100"})
        return _FakeResult()

    def commit(self):
        pass


def _client():
    complemento._ASSISTENTE_MEM.clear()
    complemento._BLOQUEIOS_MEM.clear()
    complemento._CARRINHOS_MEM.clear()
    complemento._MOTOBOYS_MEM.clear()
    complemento._CUPONS_MEM.clear()
    complemento._FIDELIDADE_MEM.clear()
    whatsapp._NOTIFICACOES.clear()
    main.app.dependency_overrides[get_conn] = lambda: _FakeConn()
    main.app.dependency_overrides[contexto] = lambda: Ctx(
        estab="11111111-1111-1111-1111-111111111111",
        perfil="gerente",
        usuario="Gerente",
    )
    return TestClient(main.app)


def teardown_function():
    main.app.dependency_overrides.clear()


def test_assistente_e_bloqueio_whatsapp():
    c = _client()
    r = c.put("/ia/assistente", json={"nome": "Bia", "voz_tts": "shimmer", "formalidade": "informal", "estilo": "Amigável", "tempo_recuperacao_min": 10})
    assert r.status_code == 200 and r.json()["nome"] == "Bia"

    c.post("/ia/bloqueios", json={"telefone": "83988880000"})
    wh = c.post("/webhook/whatsapp", json={"telefone": "83988880000", "nome": "Carla", "texto": "Oi"})
    assert wh.status_code == 200 and wh.json()["bloqueado"] is True
    assert len(whatsapp._NOTIFICACOES) == 0


def test_cross_sell_e_recuperacao_carrinho():
    c = _client()
    cs = c.get("/ia/cross-sell", params={"itens": "Hambúrguer"})
    assert cs.status_code == 200
    assert any(s["nome"] == "Refrigerante" for s in cs.json()["sugestoes"])

    c.post("/ia/carrinhos", json={"telefone": "83977770000", "cliente_nome": "João", "itens": [{"ficha": "Hambúrguer", "quantidade": 1}], "total": "28.00"})
    rec = c.post("/ia/carrinhos/recuperar", params={"minutos": 0})
    assert rec.status_code == 200 and rec.json()["lembretes_enviados"] == 1


def test_entrega_motoboy_e_crm():
    c = _client()
    mb = c.post("/entrega/motoboys", json={"nome": "Carlos", "telefone": "83966660000"})
    assert mb.status_code == 201
    dsp = c.post("/pedidos/1/despachar", json={"motoboy_id": mb.json()["id"], "endereco": "Av. Epitácio Pessoa, 500"})
    assert dsp.status_code == 200 and "maps.google.com" in dsp.json()["gps_link"]

    cp = c.post("/crm/cupons", json={"codigo": "VIP10", "tipo": "pct", "valor": "10", "limite_uso": 50})
    assert cp.status_code == 201
    val = c.post("/crm/cupons/validar", json={"codigo": "VIP10", "total": "100.00"})
    assert val.status_code == 200 and Decimal(val.json()["desconto"]) == Decimal("10.00")

    fid = c.post("/crm/fidelidade/creditar", json={"telefone": "83988880000", "valor_pedido": "100.00"})
    assert fid.status_code == 200 and fid.json()["pontos_total"] == 10 and Decimal(fid.json()["cashback_saldo"]) == Decimal("5.00")
