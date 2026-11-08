"""Integração com o Supabase Auth. O token do usuário é validado pelo próprio Supabase,
então a API não precisa conhecer nenhum segredo de assinatura."""
import os

import httpx
from fastapi import HTTPException


def _cfg() -> tuple[str, str]:
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_ANON_KEY", "")
    if not url or not key:
        raise HTTPException(500, "Defina SUPABASE_URL e SUPABASE_ANON_KEY")
    return url, key


def _erro(r) -> tuple[str, str]:
    try:
        d = r.json()
    except ValueError:
        d = {}
    return str(d.get("error_code") or d.get("code") or ""), str(d.get("msg") or d.get("message") or d.get("error_description") or "")


def usuario_do_token(token: str) -> dict:
    """Devolve os dados do usuário dono do token, ou 401 se o token for inválido ou expirado."""
    url, key = _cfg()
    try:
        r = httpx.get(f"{url}/auth/v1/user", headers={"Authorization": f"Bearer {token}", "apikey": key}, timeout=5)
    except httpx.HTTPError:
        raise HTTPException(503, "serviço de autenticação indisponível")
    if r.status_code != 200:
        raise HTTPException(401, "token inválido ou expirado")
    return r.json()


def entrar(email: str, senha: str) -> dict:
    url, key = _cfg()
    try:
        r = httpx.post(f"{url}/auth/v1/token?grant_type=password", headers={"apikey": key}, json={"email": email, "password": senha}, timeout=5)
    except httpx.HTTPError:
        raise HTTPException(503, "serviço de autenticação indisponível")
    if r.status_code != 200:
        code, msg = _erro(r)
        if code == "invalid_credentials":
            raise HTTPException(401, "e-mail ou senha incorretos")
        if code == "email_not_confirmed":
            raise HTTPException(403, "e-mail ainda não confirmado no Supabase")
        if "api key" in msg.lower() or code in ("invalid_api_key", "no_api_key"):
            raise HTTPException(500, "SUPABASE_ANON_KEY inválida ou de outro projeto: copie a chave publishable deste projeto")
        raise HTTPException(503, f"falha na autenticação do Supabase ({code or r.status_code}): {msg}")
    d = r.json()
    return {"access_token": d["access_token"], "expires_in": d.get("expires_in")}
