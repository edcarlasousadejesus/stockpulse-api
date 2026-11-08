"""Dados do negócio: tipo, meta de CMV, marca do cardápio digital e modelos iniciais."""
import re
from decimal import Decimal as D
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .core import Ctx, auditar, contexto, get_conn, resp, so_gestao
from .modelos import MODELOS

router = APIRouter()
Tipo = Literal["hamburgueria", "restaurante", "acai", "bar", "pizzaria", "outro"]
NAO_NULOS = {"nome", "tipo_negocio", "meta_cmv_pct", "cor_marca", "cardapio_publico"}


class EstabIn(BaseModel):
    nome: Optional[str] = Field(default=None, min_length=1, max_length=80)
    tipo_negocio: Optional[Tipo] = None
    meta_cmv_pct: Optional[D] = Field(default=None, gt=0, le=100)
    marca_nome: Optional[str] = Field(default=None, max_length=80)
    slogan: Optional[str] = Field(default=None, max_length=120)
    whatsapp: Optional[str] = None
    cor_marca: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")      # entra no CSS do cardápio público: só hexadecimal
    logo_url: Optional[str] = Field(default=None, pattern=r"^https://\S{1,480}$")      # só https
    cardapio_publico: Optional[bool] = None

    @field_validator("whatsapp")
    @classmethod
    def _zap(cls, v):
        if v is None or not v.strip():
            return None
        d = re.sub(r"\D", "", v)
        if not 10 <= len(d) <= 13:
            raise ValueError("WhatsApp inválido: informe DDD e número")
        return d


def _dados(conn, ctx: Ctx) -> dict:
    return conn.execute("select nome, slug, tipo_negocio, meta_cmv_pct, marca_nome, slogan, whatsapp, cor_marca, logo_url, cardapio_publico from estabelecimentos where id = %s", (ctx.estab,)).fetchone()


@router.get("/estabelecimento")
def ver_estabelecimento(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    return resp(_dados(conn, ctx))


@router.put("/estabelecimento")
def alterar_estabelecimento(b: EstabIn, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    d = b.model_dump(exclude_unset=True)
    if not d:
        raise HTTPException(422, "nada para alterar")
    if any(k in NAO_NULOS and v is None for k, v in d.items()):
        raise HTTPException(422, "estes campos não podem ficar vazios: " + ", ".join(sorted(k for k in d if k in NAO_NULOS and d[k] is None)))
    sets = ", ".join(f"{k} = %s" for k in d)  # os nomes vêm do modelo acima, nunca do cliente
    conn.execute(f"update estabelecimentos set {sets} where id = %s", [*d.values(), ctx.estab])
    auditar(conn, ctx, "alterou os dados do estabelecimento: " + ", ".join(d))
    conn.commit()
    return resp(_dados(conn, ctx))


class ModeloIn(BaseModel):
    tipo: Tipo


@router.post("/estabelecimento/modelo")
def aplicar_modelo(m: ModeloIn, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    """Carrega insumos, produtos e meta de CMV sugerida. Só funciona em um negócio ainda vazio (nunca apaga dados)."""
    q = conn.execute("""select (select count(*) from produtos where estabelecimento_id = %(e)s) + (select count(*) from fichas_tecnicas where estabelecimento_id = %(e)s)
                               + (select count(*) from movimentacoes_estoque where estabelecimento_id = %(e)s) n""", {"e": ctx.estab}).fetchone()["n"]
    if q:
        raise HTTPException(409, "o modelo só pode ser aplicado a um negócio sem produtos, fichas e movimentações")
    mod, ids = MODELOS[m.tipo], {}
    for nome, un, custo in mod["ins"]:
        ids[nome] = conn.execute("insert into produtos (estabelecimento_id, nome, unidade, custo_medio) values (%s, %s, %s, %s) returning id", (ctx.estab, nome, un, D(custo))).fetchone()["id"]
    for nome, cat, preco, extra, itens in mod["pr"]:
        fid = conn.execute("insert into fichas_tecnicas (estabelecimento_id, nome, categoria, preco_venda, outros_custos) values (%s, %s, %s, %s, %s) returning id",
                           (ctx.estab, nome, cat, D(preco), D(extra))).fetchone()["id"]
        for ins, qtd in itens:
            conn.execute("insert into itens_ficha (ficha_id, produto_id, quantidade) values (%s, %s, %s)", (fid, ids[ins], D(qtd)))
    conn.execute("update estabelecimentos set tipo_negocio = %s, meta_cmv_pct = %s where id = %s", (m.tipo, D(mod["meta"]), ctx.estab))
    auditar(conn, ctx, f"aplicou o modelo {m.tipo}")
    conn.commit()
    return resp({"tipo": m.tipo, "insumos": len(mod["ins"]), "produtos": len(mod["pr"]), "meta_cmv_pct": D(mod["meta"])}, 201)
