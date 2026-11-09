import pytest
from fastapi import HTTPException
from app import auth


class R:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        if self._b is None:
            raise ValueError
        return self._b


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "k")


def tentar(monkeypatch, status, body):
    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: R(status, body))
    with pytest.raises(HTTPException) as e:
        auth.entrar("a@b.com", "x")
    return e.value


def test_senha_errada_e_401(monkeypatch):
    e = tentar(monkeypatch, 400, {"code": 400, "error_code": "invalid_credentials", "msg": "Invalid login credentials"})
    assert e.status_code == 401 and "senha incorretos" in e.detail


def test_email_nao_confirmado(monkeypatch):
    assert tentar(monkeypatch, 400, {"error_code": "email_not_confirmed", "msg": "Email not confirmed"}).status_code == 403


def test_chave_invalida_nao_vira_senha_errada(monkeypatch):
    e = tentar(monkeypatch, 401, {"message": "Invalid API key", "hint": "..."})
    assert e.status_code == 503 and "SUPABASE_ANON_KEY" not in e.detail  # o cliente não vê detalhes de configuração (ficam no log)


def test_outros_erros_mostram_a_causa(monkeypatch):
    e = tentar(monkeypatch, 422, {"error_code": "validation_failed", "msg": "Unable to validate email address"})
    assert e.status_code == 503 and "validation_failed" not in e.detail


def test_sucesso(monkeypatch):
    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: R(200, {"access_token": "t", "expires_in": 3600}))
    assert auth.entrar("a@b.com", "x") == {"access_token": "t", "refresh_token": None, "expires_in": 3600}
