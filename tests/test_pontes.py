"""Testes das duas pontes externas: vitrine pública (/cardapio) e webhook WhatsApp (/webhook/whatsapp)."""
from decimal import Decimal
from fastapi.testclient import TestClient

from app import main, publico, whatsapp
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
    def __init__(self):
        self.pedidos_inseridos = []
        self.itens_inseridos = []
        self.auditorias = []
        self.ultimo_pedido = 0
        self.status_atual = "novo"

    def execute(self, sql, params=()):
        s = " ".join(sql.lower().split())
        if "from estabelecimentos" in s:
            return _FakeResult(one={
                "id": "11111111-1111-1111-1111-111111111111",
                "slug": "smash-do-ze",
                "nome": "Smash do Zé",
                "marca_nome": "Smash do Zé",
                "slogan": "O melhor smash da cidade",
                "whatsapp": "83998887766",
                "cor_marca": "#39ff6a",
                "logo_url": None,
            })
        if "from fichas_tecnicas f" in s and "not exists" in s:
            # Simula apenas itens COM insumo em estoque ("Batata Frita" ocultada por falta de insumo)
            return _FakeResult(many=[
                {"categoria": "Lanches", "nome": "Hambúrguer", "preco_venda": Decimal("28.00"), "preco_promocional": Decimal("22.40")},
                {"categoria": "Bebidas", "nome": "Refrigerante", "preco_venda": Decimal("7.00"), "preco_promocional": None},
            ])
        if "from fichas_tecnicas" in s and "where estabelecimento_id = %s and nome = %s" in s:
            nome = params[1]
            catalogo = {
                "Hambúrguer": {"id": "f1", "nome": "Hambúrguer", "preco_venda": Decimal("28.00"), "preco_promocional": Decimal("22.40"), "ativo": True},
                "Refrigerante": {"id": "f2", "nome": "Refrigerante", "preco_venda": Decimal("7.00"), "preco_promocional": None, "ativo": True},
            }
            return _FakeResult(one=catalogo.get(nome))
        if "update estabelecimentos set ultimo_pedido" in s:
            self.ultimo_pedido += 1
            return _FakeResult(one={"ultimo_pedido": self.ultimo_pedido})
        if "insert into pedidos" in s:
            self.pedidos_inseridos.append(params)
            return _FakeResult(one={"id": f"p-{len(self.pedidos_inseridos)}"})
        if "insert into itens_pedido" in s:
            self.itens_inseridos.append(params)
            return _FakeResult()
        if "insert into auditoria" in s:
            self.auditorias.append(params)
            return _FakeResult()
        if "from pedidos where estabelecimento_id = %s and numero = %s for update" in s:
            return _FakeResult(one={
                "id": "p-1",
                "status": self.status_atual,
                "cliente_nome": "Carla",
                "cliente_telefone": "83988880000",
            })
        if "select ficha_id, quantidade, preco_unit from itens_pedido" in s:
            return _FakeResult(many=[])
        if "update pedidos set status" in s:
            self.status_atual = params[0]
            return _FakeResult()
        return _FakeResult()

    def commit(self):
        pass


def _client(fake_conn):
    publico._TENTATIVAS.clear()
    whatsapp._NOTIFICACOES.clear()
    main.app.dependency_overrides[get_conn] = lambda: fake_conn
    main.app.dependency_overrides[contexto] = lambda: Ctx(
        estab="11111111-1111-1111-1111-111111111111",
        perfil="gerente",
        usuario="Gerente",
    )
    return TestClient(main.app)


def teardown_function():
    main.app.dependency_overrides.clear()


def test_vitrine_cardapio_html_e_qrcode_mesa():
    conn = _FakeConn()
    c = _client(conn)
    r = c.get("/cardapio?mesa=5")
    assert r.status_code == 200
    assert "Smash do Zé" in r.text
    assert "QR Code · Mesa 5" in r.text
    assert "Hambúrguer" in r.text
    assert "Batata Frita" not in r.text  # ocultado por falta de insumo


def test_vitrine_grava_pedido_delivery_retirada_mesa():
    conn = _FakeConn()
    c = _client(conn)
    r = c.post("/publico/smash-do-ze/pedidos", json={
        "cliente_nome": "Carla",
        "cliente_telefone": "83988880000",
        "canal": "Mesa",
        "observacao": "[Mesa: Mesa 5]",
        "itens": [{"ficha": "Hambúrguer", "quantidade": 2}],
    })
    assert r.status_code == 201
    assert r.json()["numero"] == 1
    assert Decimal(r.json()["total"]) == Decimal("44.80")
    assert conn.pedidos_inseridos[0][2] == "Mesa"


def test_webhook_whatsapp_verificacao():
    conn = _FakeConn()
    c = _client(conn)
    r_ok = c.get("/webhook/whatsapp", params={
        "hub.mode": "subscribe",
        "hub.verify_token": whatsapp.WHATSAPP_VERIFY_TOKEN,
        "hub.challenge": "12345",
    })
    assert r_ok.status_code == 200 and r_ok.text == "12345"
    r_bad = c.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "errado", "hub.challenge": "1"})
    assert r_bad.status_code == 403


def test_webhook_whatsapp_texto_consulta_e_pedido():
    conn = _FakeConn()
    c = _client(conn)
    r_menu = c.post("/webhook/whatsapp", json={"telefone": "83988880000", "nome": "Carla", "texto": "Qual o cardápio de hoje?"})
    assert r_menu.status_code == 200
    assert "Hambúrguer" in r_menu.json()["resposta"]

    r_ped = c.post("/webhook/whatsapp", json={
        "telefone": "83988880000",
        "nome": "Carla",
        "texto": "Quero confirmar 2 Hambúrguer e 1 Refrigerante no Pix",
    })
    assert r_ped.status_code == 201
    assert r_ped.json()["pedido"]["numero"] == 1
    assert Decimal(r_ped.json()["pedido"]["total"]) == Decimal("51.80")
    assert conn.pedidos_inseridos[0][2] == "WhatsApp"


def test_webhook_whatsapp_audio_ogg_transcricao_e_aviso_status(monkeypatch):
    import base64
    conn = _FakeConn()
    c = _client(conn)
    monkeypatch.setattr(whatsapp, "transcrever_audio_ogg", lambda b, filename="audio.ogg": "Quero confirmar 1 Hambúrguer no dinheiro")
    r = c.post(
        "/webhook/whatsapp",
        json={
            "telefone": "83988880000",
            "nome": "Carla",
            "audio_base64": base64.b64encode(b"OggS-fake-audio").decode(),
        },
    )
    assert r.status_code == 201
    assert r.json()["transcricao"] == "Quero confirmar 1 Hambúrguer no dinheiro"
    assert r.json()["pedido"]["numero"] == 1

    # Avançar status: preparo -> pronto -> entregue deve disparar 3 avisos automáticos
    for esperado in ("Em preparo", "Pronto", "Concluído"):
        ra = c.post("/pedidos/1/avancar")
        assert ra.status_code == 200
        assert esperado in (ra.json().get("aviso_whatsapp") or "")
