"""Contas: login, cadastro, recuperação de senha, sessão e equipe. As senhas ficam com o Supabase Auth (hash bcrypt):
a API nunca guarda nem registra senhas."""
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from . import auth, senha
from .config import Tipo
from .core import Ctx, auditar, bearer, get_conn, ip_do_cliente, resp, so_admin
from .limites import Janela

router = APIRouter()
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s.]{2,}$")
GENERICA_CADASTRO = "Se o e-mail puder ser usado, enviamos um link de confirmação. Confira a caixa de entrada e o spam."
GENERICA_RECUPERAR = "Se existir uma conta com este e-mail, enviamos as instruções de recuperação. Confira a caixa de entrada e o spam."

# Camadas contra adivinhação de senha: por (IP + e-mail), por IP e por e-mail (limites altos, contra ataque distribuído)
FALHAS_PAR, FALHAS_IP, FALHAS_EMAIL = Janela(5, 900), Janela(30, 900), Janela(30, 900)
CADASTROS_IP, RECUPERAR_IP, RECUPERAR_EMAIL = Janela(5, 3600), Janela(5, 3600), Janela(3, 3600)
REDEFINIR_IP, CONVITES = Janela(10, 3600), Janela(20, 3600)


def resetar_limites() -> None:
    for j in (FALHAS_PAR, FALHAS_IP, FALHAS_EMAIL, CADASTROS_IP, RECUPERAR_IP, RECUPERAR_EMAIL, REDEFINIR_IP, CONVITES):
        j.limpar()


def _email(v: str) -> str:
    v = (v or "").strip().lower()
    if len(v) > 254 or not EMAIL.match(v):
        raise ValueError("e-mail inválido")
    return v


class LoginIn(BaseModel):
    email: str
    senha: str = Field(min_length=1, max_length=128)
    _e = field_validator("email")(lambda cls, v: _email(v))


class CadastroIn(BaseModel):
    nome: str = Field(min_length=3, max_length=80)
    email: str
    senha: str = Field(min_length=1, max_length=128)
    confirmar_senha: str = Field(max_length=128)
    nome_negocio: Optional[str] = Field(default=None, max_length=80)
    tipo_negocio: Optional[Tipo] = None
    captcha_token: Optional[str] = Field(default=None, max_length=2000)
    _e = field_validator("email")(lambda cls, v: _email(v))

    @field_validator("nome")
    @classmethod
    def _nome(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v.split()) < 2 or any(len(t) < 2 for t in v.split()):
            raise ValueError("informe o nome completo (nome e sobrenome)")
        return v

    @model_validator(mode="after")
    def _confere(self):
        if self.senha != self.confirmar_senha:
            raise ValueError("a confirmação da senha não confere")
        return self


class EmailIn(BaseModel):
    email: str
    _e = field_validator("email")(lambda cls, v: _email(v))


class RenovarIn(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=2000)


class RedefinirIn(BaseModel):
    nova_senha: str = Field(min_length=1, max_length=128)
    confirmar_senha: str = Field(max_length=128)

    @model_validator(mode="after")
    def _confere(self):
        if self.nova_senha != self.confirmar_senha:
            raise ValueError("a confirmação da senha não confere")
        return self


def _exigir_senha_forte(s: str, email: str = "", nome: str = "") -> None:
    p = senha.problemas(s, email, nome)
    if p:
        raise HTTPException(422, "Senha fraca: " + "; ".join(p) + ".")


def _sessao(conn, t: dict) -> dict:
    u = auth.usuario_do_token(t["access_token"])
    row = conn.execute("select nome, perfil::text perfil, estabelecimento_id::text estab from usuarios where id = %s", (u["id"],)).fetchone()
    if row is None:
        raise HTTPException(403, "esta conta ainda não está vinculada a um estabelecimento")
    return {**t, "usuario": {"nome": row["nome"], "perfil": row["perfil"], "estabelecimento_id": row["estab"]}}


@router.post("/auth/entrar")
def entrar(b: LoginIn, req: Request, conn=Depends(get_conn)):
    """Login com e-mail e senha. A mensagem de erro é a mesma para e-mail inexistente e senha errada."""
    ip = ip_do_cliente(req)
    par = (ip, b.email)
    FALHAS_PAR.checar(par); FALHAS_IP.checar(ip); FALHAS_EMAIL.checar(b.email)
    try:
        t = auth.entrar(b.email, b.senha)
    except HTTPException as e:
        if e.status_code == 401:
            FALHAS_PAR.registrar(par); FALHAS_IP.registrar(ip); FALHAS_EMAIL.registrar(b.email)
        raise
    FALHAS_PAR.zerar(par)
    return resp(_sessao(conn, t))


@router.post("/auth/renovar")
def renovar(b: RenovarIn, conn=Depends(get_conn)):
    """Troca o refresh_token por um novo par de tokens (é assim que a sessão persiste sem pedir a senha de novo)."""
    return resp(_sessao(conn, auth.renovar(b.refresh_token)))


@router.post("/auth/sair")
def sair(creds=Depends(bearer)):
    if creds is not None:
        auth.sair(creds.credentials)
    return resp({"ok": True})


@router.post("/auth/cadastro")
def cadastro(b: CadastroIn, req: Request):
    """Cadastro público. Quem se cadastra recebe um estabelecimento PRÓPRIO e é administrador só dele (feito por gatilho no banco).
    A pessoa não escolhe perfil nem estabelecimento: campos assim, se enviados, são ignorados."""
    ip = ip_do_cliente(req)
    CADASTROS_IP.checar(ip); CADASTROS_IP.registrar(ip)
    _exigir_senha_forte(b.senha, b.email, b.nome)
    meta = {"nome": b.nome}
    if b.nome_negocio and b.nome_negocio.strip():
        meta["nome_negocio"] = b.nome_negocio.strip()
    if b.tipo_negocio:
        meta["tipo_negocio"] = b.tipo_negocio
    auth.cadastrar(b.email, b.senha, meta, b.captcha_token)
    return resp({"mensagem": GENERICA_CADASTRO}, 202)


@router.post("/auth/recuperar")
def recuperar(b: EmailIn, req: Request):
    ip = ip_do_cliente(req)
    RECUPERAR_IP.checar(ip); RECUPERAR_IP.registrar(ip)
    if RECUPERAR_EMAIL.contar(b.email) < RECUPERAR_EMAIL.maximo:  # acima do limite, ignora em silêncio: evita enchente de e-mails
        RECUPERAR_EMAIL.registrar(b.email)
        auth.recuperar(b.email)
    return resp({"mensagem": GENERICA_RECUPERAR}, 202)


@router.post("/auth/redefinir-senha")
def redefinir(b: RedefinirIn, req: Request, creds=Depends(bearer)):
    """Usa o token do link de recuperação (Authorization: Bearer ...) para definir a nova senha."""
    if creds is None:
        raise HTTPException(401, "link inválido ou expirado: peça uma nova recuperação")
    ip = ip_do_cliente(req)
    REDEFINIR_IP.checar(ip); REDEFINIR_IP.registrar(ip)
    u = auth.usuario_do_token(creds.credentials)
    _exigir_senha_forte(b.nova_senha, u.get("email") or "", (u.get("user_metadata") or {}).get("nome") or "")
    auth.redefinir(creds.credentials, b.nova_senha)
    return resp({"mensagem": "Senha alterada. Entre com a nova senha."})


# ---------- equipe (somente administrador) ----------
PerfilEquipe = Literal["administrador", "gerente", "operador"]


class ConviteIn(BaseModel):
    nome: str = Field(min_length=2, max_length=80)
    email: str
    perfil: PerfilEquipe = "operador"
    _e = field_validator("email")(lambda cls, v: _email(v))


class PerfilIn(BaseModel):
    perfil: PerfilEquipe


def _membro(conn, ctx: Ctx, uid: str) -> dict:
    try:
        m = conn.execute("select u.id::text id, u.perfil::text perfil, u.nome from usuarios u where u.id = %s and u.estabelecimento_id = %s", (uid, ctx.estab)).fetchone()
    except Exception:
        conn.rollback()
        m = None
    if m is None:
        raise HTTPException(404, "pessoa não encontrada na sua equipe")
    return m


def _admins(conn, ctx: Ctx) -> int:
    return conn.execute("select count(*) n from usuarios where estabelecimento_id = %s and perfil = 'administrador'", (ctx.estab,)).fetchone()["n"]


@router.get("/equipe")
def equipe(ctx: Ctx = Depends(so_admin), conn=Depends(get_conn)):
    rows = conn.execute("select u.id::text id, u.nome, u.perfil::text perfil, a.email from usuarios u left join auth.users a on a.id = u.id where u.estabelecimento_id = %s order by u.nome", (ctx.estab,)).fetchall()
    return resp(rows)


@router.post("/equipe/convites")
def convidar(b: ConviteIn, ctx: Ctx = Depends(so_admin), conn=Depends(get_conn)):
    """O administrador convida alguém para o PRÓPRIO estabelecimento, escolhendo o perfil. O perfil é definido aqui, no servidor."""
    CONVITES.checar(ctx.estab); CONVITES.registrar(ctx.estab)
    auth.convidar(b.email, b.nome.strip(), ctx.estab, b.perfil)
    auditar(conn, ctx, f"convidou {b.email} como {b.perfil}")
    conn.commit()
    return resp({"mensagem": "Convite enviado: a pessoa receberá um e-mail para definir a senha."}, 202)


@router.put("/equipe/{uid}/perfil")
def mudar_perfil(uid: str, b: PerfilIn, ctx: Ctx = Depends(so_admin), conn=Depends(get_conn)):
    m = _membro(conn, ctx, uid)
    if m["perfil"] == "administrador" and b.perfil != "administrador" and _admins(conn, ctx) <= 1:
        raise HTTPException(409, "o estabelecimento precisa de ao menos um administrador")
    conn.execute("update usuarios set perfil = %s::perfil where id = %s", (b.perfil, uid))
    auditar(conn, ctx, f"mudou o perfil de {m['nome']} para {b.perfil}")
    conn.commit()
    return resp({"id": uid, "perfil": b.perfil})


@router.delete("/equipe/{uid}")
def remover(uid: str, ctx: Ctx = Depends(so_admin), conn=Depends(get_conn)):
    """Remove o acesso da pessoa a este estabelecimento (a conta de login dela continua existindo no Supabase, mas sem acesso a dados)."""
    m = _membro(conn, ctx, uid)
    if uid == ctx.uid:
        raise HTTPException(409, "você não pode remover o próprio acesso")
    if m["perfil"] == "administrador" and _admins(conn, ctx) <= 1:
        raise HTTPException(409, "o estabelecimento precisa de ao menos um administrador")
    conn.execute("delete from usuarios where id = %s", (uid,))
    auditar(conn, ctx, f"removeu o acesso de {m['nome']}")
    conn.commit()
    return resp({"removido": uid})
