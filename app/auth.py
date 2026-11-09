"""Integração com o Supabase Auth. O token do usuário é validado pelo próprio Supabase,
então a API não precisa conhecer nenhum segredo de assinatura."""
import logging
import os

import httpx
from fastapi import HTTPException

log = logging.getLogger("stockpulse")


def _cfg() -> tuple[str, str]:
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_ANON_KEY", "")
    if not url or not key:
        log.error("autenticação sem configuração: defina SUPABASE_URL e SUPABASE_ANON_KEY")
        raise HTTPException(503, "autenticação não configurada no servidor")
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
            raise HTTPException(403, "confirme seu e-mail antes de entrar: procure o link que enviamos")
        if "api key" in msg.lower() or code in ("invalid_api_key", "no_api_key"):
            log.error("SUPABASE_ANON_KEY inválida ou de outro projeto")
            raise HTTPException(503, "autenticação mal configurada no servidor")
        log.error("falha no login do Supabase (%s): %s", code or r.status_code, msg)
        raise HTTPException(503, "serviço de autenticação indisponível")
    return _tokens(r.json())


def _tokens(d: dict) -> dict:
    return {"access_token": d["access_token"], "refresh_token": d.get("refresh_token"), "expires_in": d.get("expires_in")}


def _enviar(metodo: str, caminho: str, json=None, token: str | None = None, chave: str | None = None):
    url, key = _cfg()
    h = {"apikey": chave or key}
    if token or chave:
        h["Authorization"] = f"Bearer {token or chave}"
    try:
        return httpx.request(metodo, f"{url}{caminho}", headers=h, json=json, timeout=8)
    except httpx.HTTPError as e:
        log.error("Supabase Auth inacessível: %s", type(e).__name__)
        raise HTTPException(503, "serviço de autenticação indisponível")


def cadastrar(email: str, senha: str, meta: dict, captcha: str | None = None) -> None:
    """Cria a conta. O Supabase guarda a senha com hash e envia o e-mail de confirmação (se ligado no painel).
    E-mail já cadastrado NÃO gera erro aqui, para a resposta nunca revelar quem já tem conta."""
    corpo = {"email": email, "password": senha, "data": meta}
    if captcha:
        corpo["gotrue_meta_security"] = {"captcha_token": captcha}
    destino = os.environ.get("AUTH_REDIRECT_URL")
    r = _enviar("POST", "/auth/v1/signup" + (f"?redirect_to={destino}" if destino else ""), corpo)
    if r.status_code in (200, 201):
        return
    code, msg = _erro(r)
    if code in ("user_already_exists", "email_exists") or "already registered" in msg.lower():
        return
    if code == "weak_password":
        raise HTTPException(422, "a senha não atende à política de segurança")
    if code in ("email_address_invalid", "validation_failed"):
        raise HTTPException(422, "e-mail inválido")
    if r.status_code == 429 or code in ("over_email_send_rate_limit", "over_request_rate_limit"):
        raise HTTPException(429, "muitas tentativas; aguarde alguns minutos e tente de novo")
    if code in ("captcha_failed",):
        raise HTTPException(400, "não foi possível validar o CAPTCHA")
    log.error("falha no cadastro do Supabase (%s): %s", code or r.status_code, msg)
    raise HTTPException(503, "serviço de autenticação indisponível")


def recuperar(email: str) -> None:
    """Envia o e-mail de recuperação. Nunca revela se o e-mail existe: erros de 'não encontrado' são ignorados."""
    destino = os.environ.get("AUTH_REDIRECT_URL")
    r = _enviar("POST", "/auth/v1/recover" + (f"?redirect_to={destino}" if destino else ""), {"email": email})
    if r.status_code == 429:
        raise HTTPException(429, "muitas tentativas; aguarde alguns minutos e tente de novo")
    if r.status_code >= 500:
        log.error("falha ao enviar recuperação (%s)", r.status_code)
        raise HTTPException(503, "serviço de autenticação indisponível")


def renovar(refresh_token: str) -> dict:
    r = _enviar("POST", "/auth/v1/token?grant_type=refresh_token", {"refresh_token": refresh_token})
    if r.status_code != 200:
        if r.status_code >= 500:
            raise HTTPException(503, "serviço de autenticação indisponível")
        raise HTTPException(401, "sessão expirada: entre novamente")
    return _tokens(r.json())


def sair(token: str) -> None:
    """Encerra a sessão no Supabase (invalida os tokens de renovação). Se falhar, o logout local continua valendo."""
    try:
        _enviar("POST", "/auth/v1/logout", token=token)
    except HTTPException:
        pass


def redefinir(token: str, nova_senha: str) -> None:
    r = _enviar("PUT", "/auth/v1/user", {"password": nova_senha}, token=token)
    if r.status_code == 200:
        return
    code, _ = _erro(r)
    if code == "weak_password":
        raise HTTPException(422, "a senha não atende à política de segurança")
    if code == "same_password":
        raise HTTPException(422, "escolha uma senha diferente da atual")
    if r.status_code in (401, 403):
        raise HTTPException(401, "link inválido ou expirado: peça uma nova recuperação")
    raise HTTPException(503, "serviço de autenticação indisponível")


def convidar(email: str, nome: str, estab: str, perfil: str) -> None:
    """Cria o usuário já vinculado ao estabelecimento e envia o link para ele definir a senha.
    O vínculo e o perfil vão em app_metadata (só o servidor escreve ali) e exigem a chave de administrador, que NUNCA vai ao navegador."""
    svc = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not svc:
        raise HTTPException(503, "convites não configurados no servidor")
    r = _enviar("POST", "/auth/v1/admin/users", {"email": email, "email_confirm": True, "app_metadata": {"estabelecimento_id": estab, "perfil": perfil, "nome": nome},
                                                 "user_metadata": {"nome": nome}}, chave=svc)
    if r.status_code not in (200, 201):
        code, _ = _erro(r)
        if code in ("email_exists", "user_already_exists") or r.status_code == 422:
            raise HTTPException(409, "não foi possível convidar este e-mail")
        log.error("falha ao convidar (%s)", code or r.status_code)
        raise HTTPException(503, "serviço de autenticação indisponível")
    recuperar(email)


def diagnostico() -> dict:
    """Confere a chave e as configurações do projeto no Supabase, sem expor nenhum segredo."""
    try:
        url, key = _cfg()
    except HTTPException:
        return {"supabase": "nao_configurado", "confirmacao_de_email": None}
    try:
        r = httpx.get(f"{url}/auth/v1/settings", headers={"apikey": key}, timeout=5)
    except httpx.HTTPError:
        return {"supabase": "inacessivel", "confirmacao_de_email": None}
    if r.status_code in (401, 403):
        return {"supabase": "chave_invalida", "confirmacao_de_email": None}
    if r.status_code != 200:
        return {"supabase": "erro", "confirmacao_de_email": None}
    return {"supabase": "ok", "confirmacao_de_email": (not r.json().get("mailer_autoconfirm")) if "mailer_autoconfirm" in r.json() else None}
