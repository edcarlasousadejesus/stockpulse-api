"""Pedidos (quadro da cozinha) e clientes. Ao concluir, o pedido vira venda, entra no CMV e baixa o estoque."""
import re
from decimal import Decimal as D
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from . import calc
from .core import Ctx, auditar, contexto, fuso, get_conn, hoje, resp
from .operacao import registrar_venda

router = APIRouter()
FLUXO = ["novo", "preparo", "pronto", "entregue"]
Canal = Literal["Balcão", "Delivery", "WhatsApp", "Mesa", "Cardápio"]
Pagamento = Literal["Pix", "Cartão", "Dinheiro", "A combinar"]
Status = Literal["novo", "preparo", "pronto", "entregue", "cancelado"]


def telefone(v: Optional[str]) -> Optional[str]:
    if v is None or not str(v).strip():
        return None
    d = re.sub(r"\D", "", v)
    if not 10 <= len(d) <= 13:
        raise ValueError("telefone inválido: informe DDD e número")
    return d


class ItemPedidoIn(BaseModel):
    ficha: str = Field(min_length=1, max_length=80)
    quantidade: D = Field(gt=0, le=99)


class PedidoIn(BaseModel):
    cliente_nome: str = Field(min_length=1, max_length=80)
    cliente_telefone: Optional[str] = None
    canal: Canal = "Balcão"
    pagamento: Pagamento = "A combinar"
    observacao: Optional[str] = Field(default=None, max_length=300)
    itens: list[ItemPedidoIn] = Field(min_length=1, max_length=30)

    _tel = field_validator("cliente_telefone")(lambda cls, v: telefone(v))


def criar_pedido(conn, estab: str, nome: str, tel: Optional[str], canal: str, pagamento: str, obs: Optional[str], itens: list[ItemPedidoIn]) -> dict:
    """Os preços SEMPRE vêm do banco: o cliente informa só o produto e a quantidade."""
    agrupado: dict[str, D] = {}
    for i in itens:
        agrupado[i.ficha] = agrupado.get(i.ficha, D(0)) + i.quantidade
    linhas = []
    for nome_f, q in agrupado.items():
        f = conn.execute("select id, nome, preco_venda, preco_promocional, ativo from fichas_tecnicas where estabelecimento_id = %s and nome = %s", (estab, nome_f)).fetchone()
        preco = (f["preco_promocional"] or f["preco_venda"]) if f else None
        if f is None or not f["ativo"] or not preco:
            raise HTTPException(422, f"produto indisponível: {nome_f}")
        if q > 99:
            raise HTTPException(422, f"quantidade máxima por item: 99 ({nome_f})")
        linhas.append((f, q, preco))
    total = calc.money(sum((q * p for _, q, p in linhas), D(0)))
    numero = conn.execute("update estabelecimentos set ultimo_pedido = ultimo_pedido + 1 where id = %s returning ultimo_pedido", (estab,)).fetchone()["ultimo_pedido"]
    pid = conn.execute(
        "insert into pedidos (estabelecimento_id, numero, canal, cliente_nome, cliente_telefone, pagamento, observacao, total) values (%s, %s, %s, %s, %s, %s, %s, %s) returning id",
        (estab, numero, canal, nome.strip(), tel, pagamento, obs, total)).fetchone()["id"]
    for f, q, p in linhas:
        conn.execute("insert into itens_pedido (pedido_id, ficha_id, nome, quantidade, preco_unit) values (%s, %s, %s, %s, %s)", (pid, f["id"], f["nome"], q, p))
    return {"numero": numero, "total": total}


def _carregar(conn, ctx: Ctx, status: Optional[str], dia, limite: int) -> list[dict]:
    sql, par = "select id, numero, canal, status, cliente_nome, cliente_telefone, pagamento, observacao, total, visto, criado_em, concluido_em from pedidos where estabelecimento_id = %s", [ctx.estab]
    if status:
        sql += " and status = %s"; par.append(status)
    if dia:
        sql += " and (criado_em at time zone %s)::date = %s"; par += [fuso(), dia]
    rows = conn.execute(sql + " order by criado_em desc, numero desc limit %s", par + [limite]).fetchall()
    itens = conn.execute("select pedido_id, nome, quantidade, preco_unit from itens_pedido where pedido_id = any(%s) order by nome", ([r["id"] for r in rows],)).fetchall() if rows else []
    for r in rows:
        r["itens"] = [{"nome": i["nome"], "quantidade": i["quantidade"], "preco_unit": i["preco_unit"]} for i in itens if i["pedido_id"] == r["id"]]
        del r["id"]
    return rows


@router.post("/pedidos")
def novo_pedido(p: PedidoIn, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    r = criar_pedido(conn, ctx.estab, p.cliente_nome, p.cliente_telefone, p.canal, p.pagamento, p.observacao, p.itens)
    auditar(conn, ctx, f"registrou o pedido #{r['numero']} de {p.cliente_nome}")
    conn.commit()
    return resp(r, 201)


@router.get("/pedidos")
def listar_pedidos(status: Optional[Status] = None, dia: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"), limite: int = Query(100, ge=1, le=500),
                   ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    return resp(_carregar(conn, ctx, status, dia, limite))


def _travar(conn, ctx: Ctx, numero: int) -> dict:
    p = conn.execute("select id, status from pedidos where estabelecimento_id = %s and numero = %s for update", (ctx.estab, numero)).fetchone()
    if p is None:
        raise HTTPException(404, "pedido não encontrado")
    return p


@router.post("/pedidos/{numero}/avancar")
def avancar(numero: int, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    """novo → preparo → pronto → entregue. Ao entregar, vira venda e baixa o estoque."""
    p = _travar(conn, ctx, numero)
    if p["status"] in ("cancelado", "entregue"):
        raise HTTPException(409, f"pedido {p['status']}: não pode avançar")
    novo, negativos = FLUXO[FLUXO.index(p["status"]) + 1], []
    if novo == "entregue":
        dia = hoje()
        for it in conn.execute("select ficha_id, quantidade, preco_unit from itens_pedido where pedido_id = %s", (p["id"],)).fetchall():
            if it["ficha_id"] is None:
                continue  # produto removido depois do pedido: segue sem baixa
            f = conn.execute("select id, outros_custos from fichas_tecnicas where id = %s", (it["ficha_id"],)).fetchone()
            negativos += registrar_venda(conn, ctx, f, it["quantidade"], it["preco_unit"], dia, p["id"])[1]
        conn.execute("update pedidos set status = %s, concluido_em = now(), visto = true where id = %s", (novo, p["id"]))
    else:
        conn.execute("update pedidos set status = %s, visto = true where id = %s", (novo, p["id"]))
    auditar(conn, ctx, f"pedido #{numero}: {novo}")
    conn.commit()
    return resp({"numero": numero, "status": novo, "estoque_negativo": sorted(set(negativos))})


@router.post("/pedidos/{numero}/cancelar")
def cancelar(numero: int, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    p = _travar(conn, ctx, numero)
    if p["status"] in ("entregue", "cancelado"):
        raise HTTPException(409, f"pedido {p['status']}: não pode ser cancelado")
    conn.execute("update pedidos set status = 'cancelado', visto = true where id = %s", (p["id"],))
    auditar(conn, ctx, f"cancelou o pedido #{numero}")
    conn.commit()
    return resp({"numero": numero, "status": "cancelado"})


@router.post("/pedidos/{numero}/visto")
def marcar_visto(numero: int, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    p = _travar(conn, ctx, numero)
    conn.execute("update pedidos set visto = true where id = %s", (p["id"],))
    conn.commit()
    return resp({"numero": numero, "visto": True})


@router.get("/clientes")
def clientes(dias_recuperar: int = Query(15, ge=1, le=365), ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    """Clientes agrupados por telefone (ou nome). Quem está há `dias_recuperar` dias ou mais sem pedir fica como 'recuperar'."""
    chave = "coalesce(nullif(p.cliente_telefone, ''), p.cliente_nome)"
    rows = conn.execute(
        f"""select {chave} chave, max(p.cliente_nome) nome, max(p.cliente_telefone) telefone, count(*) pedidos, sum(p.total) total,
                   (max(p.criado_em) at time zone %s)::date ultima
            from pedidos p where p.estabelecimento_id = %s and p.status <> 'cancelado' group by 1""", (fuso(), ctx.estab)).fetchall()
    fav: dict = {}
    for r in conn.execute(f"""select {chave} chave, i.nome, sum(i.quantidade) q from itens_pedido i join pedidos p on p.id = i.pedido_id
                              where p.estabelecimento_id = %s and p.status <> 'cancelado' group by 1, 2""", (ctx.estab,)).fetchall():
        if r["chave"] not in fav or r["q"] > fav[r["chave"]][1]:
            fav[r["chave"]] = (r["nome"], r["q"])
    h, out = hoje(), []
    for r in rows:
        dias = (h - r["ultima"]).days
        out.append({"nome": r["nome"], "telefone": r["telefone"], "pedidos": r["pedidos"], "total": calc.money(r["total"]),
                    "ticket_medio": calc.money(r["total"] / r["pedidos"]), "ultima_compra": r["ultima"], "dias_sem_pedir": dias,
                    "favorito": fav.get(r["chave"], (None,))[0], "situacao": "recuperar" if dias >= dias_recuperar else "ativo"})
    return resp(sorted(out, key=lambda x: -x["dias_sem_pedir"]))
