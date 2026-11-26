"""Testes das melhorias Etapa 2: geração de arte com IA, eventos de pedido, TTS WhatsApp e controle de cota SaaS."""
from decimal import Decimal
from fastapi.testclient import TestClient

from app import ia, main, whatsapp
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
    def __init__(self, plano="starter", ia_ativo=True):
        self.plano = plano
        self.ia_ativo = ia_ativo
        self.status_atual = "novo"
        self.auditorias = []
        self.usos = []

    def execute(self, sql, params=()):
        s = " ".join(sql.lower().split())
        if "select plano, ia_ativo from estabelecimentos" in s:
            return _FakeResult(one={"plano": self.plano, "ia_ativo": self.ia_ativo})
        if "from uso_ia" in s:
            return _FakeResult(one={"total": 0})
        if "insert into uso_ia" in s:
            self.usos.append(params)
            return _FakeResult()
        if "from pedidos where estabelecimento_id = %s and numero = %s" in s:
            return _FakeResult(one={
                "id": "p-1",
                "status": self.status_atual,
                "cliente_nome": "Carla",
                "cliente_telefone": "83988880000",
            })
        if "update pedidos set status" in s:
            self.status_atual = params[0]
            return _FakeResult()
        if "insert into auditoria" in s:
            self.auditorias.append(params)
            return _FakeResult()
        if "avg(tempo_preparo_min)" in s:
            return _FakeResult(one={"media": 18})
        return _FakeResult()

    def commit(self):
        pass


def _client(conn):
    ia._USO_MEM.clear()
    whatsapp._NOTIFICACOES.clear()
    main.app.dependency_overrides[get_conn] = lambda: conn
    main.app.dependency_overrides[contexto] = lambda: Ctx(
        estab="11111111-1111-1111-1111-111111111111",
        perfil="gerente",
        usuario="Gerente",
        uid="22222222-2222-2222-2222-222222222222",
    )
    return TestClient(main.app)


def teardown_function():
    main.app.dependency_overrides.clear()


def test_ia_gerar_arte_e_cota_saas(monkeypatch):
    conn = _FakeConn(plano="free")
    c = _client(conn)
    monkeypatch.setattr(ia, "gerar_imagem_ia", lambda prompt: ("ZmFrZS1wbmc=", "gemini"))

    payload = {
        "produto": "Hambúrguer da casa",
        "categoria": "Lanches",
        "ingredientes": [{"nome": "Carne bovina", "quantidade": "0.15"}],
        "preco_normal": "28.00",
        "preco_promocional": "21.90",
        "titulo": "Quarta do Burger",
        "validade": "Só hoje",
        "marca": {"nome": "Smash do Zé", "cor": "#7fee64"},
    }
    r = c.post("/ia/gerar-arte", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["imagem_base64"] == "ZmFrZS1wbmc="
    assert body["provedor"] == "gemini"
    assert body["uso"]["plano"] == "free"
    assert body["uso"]["usado_hoje"] == 1

    uso = c.get("/ia/uso")
    assert uso.status_code == 200
    assert uso.json()["plano"] == "free"


def test_pedido_evento_dispara_tts_e_fallback(monkeypatch):
    conn = _FakeConn()
    c = _client(conn)

    # 1) Com TTS ativo -> registra envio de áudio
    monkeypatch.setattr(whatsapp, "gerar_audio_tts", lambda txt, voice=None: (b"OggS-tts", "openai"))
    r = c.post("/pedidos/evento", json={
        "numero": 1,
        "status": "preparo",
        "cliente_nome": "Carla",
        "cliente_telefone": "83988880000",
    })
    assert r.status_code == 200
    assert "Em preparo" in (r.json()["aviso_whatsapp"] or "")
    assert whatsapp._NOTIFICACOES[-1]["tipo"] == "audio"

    # 2) Sem TTS -> fallback automático em texto
    monkeypatch.setattr(whatsapp, "gerar_audio_tts", lambda txt, voice=None: None)
    r2 = c.post("/pedidos/evento", json={
        "numero": 1,
        "status": "pronto",
        "cliente_nome": "Carla",
        "cliente_telefone": "83988880000",
        "tempo_preparo_min": 14,
    })
    assert r2.status_code == 200
    assert "Pronto" in (r2.json()["aviso_whatsapp"] or "")
    assert whatsapp._NOTIFICACOES[-1]["tipo"] == "texto"
