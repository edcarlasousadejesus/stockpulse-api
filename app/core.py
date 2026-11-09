"""Infraestrutura: conexão com o PostgreSQL, contexto do usuário, permissões e resposta JSON."""
import json
import logging
import os
import uuid
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import Response
from fastapi.security import HTTPBearer
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from . import auth

PERFIS = {"administrador", "gerente", "operador"}
log = logging.getLogger("stockpulse")
_pool: ConnectionPool | None = None
bearer = HTTPBearer(auto_error=False, description="Token de acesso obtido em POST /auth/entrar")


def pool() -> ConnectionPool:
    """Cria o pool na primeira chamada. Use a URL do pooler do Supabase em DATABASE_URL."""
    global _pool
    if _pool is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("Defina a variável de ambiente DATABASE_URL")
        _pool = ConnectionPool(url, min_size=1, max_size=5, open=True,
                               kwargs={"row_factory": dict_row, "prepare_threshold": None})
    return _pool


def get_conn():
    """Conexão do pedido. Falha de configuração ou de rede vira 503 com mensagem clara (e log no servidor), não um 500 sem explicação."""
    try:
        cm = pool().connection(timeout=8)
        conn = cm.__enter__()
    except RuntimeError as e:
        log.error("configuração do banco ausente: %s", e)
        raise HTTPException(503, "servidor sem configuração de banco de dados (DATABASE_URL)")
    except Exception as e:
        log.error("banco de dados indisponível: %s: %s", type(e).__name__, str(e)[:200])
        raise HTTPException(503, "banco de dados indisponível")
    try:
        yield conn
    except BaseException as e:
        cm.__exit__(type(e), e, e.__traceback__)
        raise
    else:
        cm.__exit__(None, None, None)


def ip_do_cliente(req: Request) -> str:
    # Atrás de proxy, o IP real vem em X-Forwarded-For. Só confie nele se o seu proxy o define (STOCKPULSE_PROXY=1).
    if os.environ.get("STOCKPULSE_PROXY") == "1" and req.headers.get("x-forwarded-for"):
        return req.headers["x-forwarded-for"].split(",")[0].strip()
    return req.client.host if req.client else "?"


def fuso() -> str:
    return os.environ.get("STOCKPULSE_TZ", "America/Sao_Paulo")


def hoje() -> date:
    """Data de hoje no fuso do negócio (o servidor costuma rodar em UTC)."""
    return datetime.now(ZoneInfo(fuso())).date()


def resp(obj, status: int = 200) -> Response:
    """Dinheiro e quantidades saem como texto exato ("1500.00"), nunca como float."""
    enc = lambda o: format(o, "f") if isinstance(o, Decimal) else o.isoformat()
    return Response(json.dumps(obj, default=enc, ensure_ascii=False), status_code=status, media_type="application/json")


class Ctx(BaseModel):
    estab: str
    perfil: str
    usuario: str
    uid: str | None = None


def contexto(
    creds=Depends(bearer),
    x_estabelecimento: str | None = Header(None),
    x_perfil: str | None = Header(None),
    x_usuario: str = Header("anonimo"),
    conn=Depends(get_conn),
) -> Ctx:
    """Quem é o usuário e a qual estabelecimento ele pertence.

    Normal: token do Supabase Auth (Authorization: Bearer ...) + linha na tabela usuarios.
    Só para desenvolvimento e testes: STOCKPULSE_AUTH=cabecalhos aceita X-Estabelecimento/X-Perfil.
    """
    if os.environ.get("STOCKPULSE_AUTH") == "cabecalhos":
        if not x_estabelecimento or not x_perfil:
            raise HTTPException(422, "cabeçalhos X-Estabelecimento e X-Perfil são obrigatórios neste modo")
        if x_perfil not in PERFIS:
            raise HTTPException(401, "perfil inválido")
        try:
            uuid.UUID(x_estabelecimento)
        except ValueError:
            raise HTTPException(401, "X-Estabelecimento deve ser o UUID de um estabelecimento")
        if conn.execute("select 1 from estabelecimentos where id = %s", (x_estabelecimento,)).fetchone() is None:
            raise HTTPException(401, "estabelecimento não encontrado")
        return Ctx(estab=x_estabelecimento, perfil=x_perfil, usuario=x_usuario)
    if creds is None:
        raise HTTPException(401, "faça login: envie o token em Authorization: Bearer")
    u = auth.usuario_do_token(creds.credentials)
    row = conn.execute("select id::text uid, nome, perfil::text perfil, estabelecimento_id::text estab from usuarios where id = %s", (u["id"],)).fetchone()
    if row is None:
        raise HTTPException(403, "usuário sem acesso a nenhum estabelecimento")
    return Ctx(estab=row["estab"], perfil=row["perfil"], usuario=row["nome"], uid=row["uid"])


def so_gestao(ctx: Ctx = Depends(contexto)) -> Ctx:
    if ctx.perfil == "operador":
        raise HTTPException(403, "apenas gerente ou administrador")
    return ctx


def so_admin(ctx: Ctx = Depends(contexto)) -> Ctx:
    if ctx.perfil != "administrador":
        raise HTTPException(403, "apenas administrador")
    return ctx


def auditar(conn, ctx: Ctx, acao: str, motivo: str | None = None) -> None:
    conn.execute(
        "insert into auditoria (estabelecimento_id, usuario_id, usuario_nome, perfil, acao, motivo) values (%s, %s, %s, %s, %s, %s)",
        (ctx.estab, ctx.uid, ctx.usuario, ctx.perfil, acao, motivo),
    )
