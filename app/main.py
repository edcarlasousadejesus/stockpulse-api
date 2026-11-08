import os
from datetime import date
from decimal import Decimal
from typing import Optional

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import auth, calc, config, operacao, pedidos, publico
from .core import Ctx, auditar, contexto, get_conn, resp, so_gestao

PRODUCAO = os.environ.get("STOCKPULSE_ENV") == "producao"
# Em produção a documentação interativa fica desligada (ela mostra toda a superfície da API)
app = FastAPI(title="StockPulse API", version="0.4.0", docs_url=None if PRODUCAO else "/docs",
              redoc_url=None if PRODUCAO else "/redoc", openapi_url=None if PRODUCAO else "/openapi.json")
ORIGENS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
if ORIGENS:  # endereços do seu site que podem chamar a API pelo navegador
    app.add_middleware(CORSMiddleware, allow_origins=ORIGENS, allow_methods=["GET", "POST", "PUT"], allow_headers=["Authorization", "Content-Type"], allow_credentials=False)
for r in (operacao.router, pedidos.router, config.router, publico.router):
    app.include_router(r)


@app.get("/")
def raiz():
    return {"servico": "StockPulse API", "versao": "0.4.0"}


class LoginIn(BaseModel):
    email: str = Field(min_length=3)
    senha: str = Field(min_length=1)


@app.post("/auth/entrar")
def entrar(b: LoginIn, conn=Depends(get_conn)):
    """Login com e-mail e senha cadastrados no Supabase Auth. Devolve o token para as demais rotas."""
    t = auth.entrar(b.email, b.senha)
    u = auth.usuario_do_token(t["access_token"])
    row = conn.execute("select nome, perfil::text perfil, estabelecimento_id::text estab from usuarios where id = %s", (u["id"],)).fetchone()
    if row is None:
        raise HTTPException(403, "usuário sem acesso a nenhum estabelecimento")
    return resp({**t, "usuario": {"nome": row["nome"], "perfil": row["perfil"], "estabelecimento_id": row["estab"]}})


@app.get("/eu")
def eu(ctx: Ctx = Depends(contexto)):
    return resp({"usuario": ctx.usuario, "perfil": ctx.perfil, "estabelecimento_id": ctx.estab})


class FechamentoIn(BaseModel):
    data: date
    faturamento: Decimal = Field(gt=0)
    estoque_inicial: Decimal = Field(ge=0)
    compras_liquidas: Decimal = Field(ge=0)
    estoque_final: Decimal = Field(ge=0)
    custo_teorico: Optional[Decimal] = Field(default=None, ge=0)
    perdas: Decimal = Field(default=Decimal("0"), ge=0)


class ReabrirIn(BaseModel):
    motivo: str = Field(min_length=5)


def meta_cmv(conn, ctx: Ctx) -> Decimal:
    return conn.execute("select meta_cmv_pct from estabelecimentos where id = %s", (ctx.estab,)).fetchone()["meta_cmv_pct"]


def apurar(f: FechamentoIn, meta: Decimal) -> dict:
    c = calc.cmv(f.estoque_inicial, f.compras_liquidas, f.estoque_final)
    pct = calc.cmv_pct(c, f.faturamento)
    out = {
        "data": f.data, "faturamento": calc.money(f.faturamento), "cmv": c, "cmv_pct": pct,
        "margem_bruta": calc.margem_bruta(f.faturamento, c), "acima_da_meta": pct > meta,
        "perdas": calc.money(f.perdas), "estoque_final": calc.money(f.estoque_final),
    }
    if f.custo_teorico is not None:
        out["divergencia"] = calc.divergencia(c, f.custo_teorico)
    return out


@app.post("/cmv/calcular")
def calcular(f: FechamentoIn, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    """Simula o CMV sem gravar nada."""
    return resp(apurar(f, meta_cmv(conn, ctx)))


@app.post("/fechamentos")
def fechar_dia(f: FechamentoIn, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    if conn.execute("select 1 from fechamentos_diarios where estabelecimento_id = %s and data = %s and status = 'fechado'", (ctx.estab, f.data)).fetchone():
        raise HTTPException(409, "dia já fechado; reabra com motivo para alterar")
    r = apurar(f, meta_cmv(conn, ctx))
    try:
        conn.execute(
            """insert into fechamentos_diarios (estabelecimento_id, data, faturamento, estoque_inicial, compras_liquidas, estoque_final,
                                                custo_teorico, perdas, fechado_por, fechado_por_nome) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (ctx.estab, f.data, f.faturamento, f.estoque_inicial, f.compras_liquidas, f.estoque_final, f.custo_teorico, f.perdas, ctx.uid, ctx.usuario))
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "dia já fechado; reabra com motivo para alterar")
    auditar(conn, ctx, f"fechou {f.data}")
    conn.commit()
    return resp(r, 201)


def _lista(conn, ctx: Ctx) -> list[dict]:
    meta = meta_cmv(conn, ctx)
    rows = conn.execute(
        """select data, faturamento, estoque_inicial, compras_liquidas, estoque_final, custo_teorico, perdas
           from fechamentos_diarios where estabelecimento_id = %s and status = 'fechado' order by data""", (ctx.estab,)).fetchall()
    return [apurar(FechamentoIn(**r), meta) for r in rows]


@app.get("/fechamentos")
def listar(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    """Cada estabelecimento enxerga apenas os próprios fechamentos."""
    return resp(_lista(conn, ctx))


@app.post("/fechamentos/{dia}/reabrir")
def reabrir(dia: date, body: ReabrirIn, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    """Preserva a fotografia do dia (status 'reaberto') e libera um novo fechamento."""
    r = conn.execute("update fechamentos_diarios set status = 'reaberto' where estabelecimento_id = %s and data = %s and status = 'fechado' returning id",
                     (ctx.estab, dia)).fetchone()
    if r is None:
        raise HTTPException(404, "não há fechamento nessa data")
    auditar(conn, ctx, f"reabriu {dia}", body.motivo)
    conn.commit()
    return resp({"reaberto": dia})


@app.get("/auditoria")
def historico(ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    rows = conn.execute("select usuario_nome usuario, perfil, acao, motivo, criado_em em from auditoria where estabelecimento_id = %s order by id", (ctx.estab,)).fetchall()
    return resp(rows)


@app.get("/relatorios/resumo")
def resumo(ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    fs = _lista(conn, ctx)
    if not fs:
        return resp({"dias": 0})
    pior = max(fs, key=lambda x: x["cmv_pct"])
    return resp({
        "dias": len(fs),
        "cmv_medio_pct": (sum(f["cmv_pct"] for f in fs) / len(fs)).quantize(Decimal("0.1")),
        "pior_dia": {"data": pior["data"], "cmv_pct": pior["cmv_pct"]},
        "perdas_total": sum(f["perdas"] for f in fs),
    })
