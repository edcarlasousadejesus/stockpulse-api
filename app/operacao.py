"""Rotas operacionais: produtos, estoque, compras, fichas técnicas, vendas e perdas."""
from datetime import date
from decimal import Decimal as D
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import calc
from .core import Ctx, auditar, contexto, get_conn, resp, so_gestao

router = APIRouter()
CUSTO4 = D("0.0001")


def achar(conn, ctx: Ctx, nome: str) -> dict:
    p = conn.execute("select id, unidade, custo_medio from produtos where estabelecimento_id = %s and nome = %s and ativo",
                     (ctx.estab, nome)).fetchone()
    if p is None:
        raise HTTPException(404, f"produto não cadastrado: {nome}")
    return p


def saldo(conn, produto_id) -> D:
    return conn.execute("select coalesce(sum(quantidade), 0) s from movimentacoes_estoque where produto_id = %s", (produto_id,)).fetchone()["s"]


def mover(conn, ctx: Ctx, produto_id, q: D, dia: date, tipo: str, origem: str) -> None:
    conn.execute(
        "insert into movimentacoes_estoque (estabelecimento_id, produto_id, tipo, quantidade, data, origem) values (%s, %s, %s, %s, %s, %s)",
        (ctx.estab, produto_id, tipo, q, dia, origem))


def valor_estoque(conn, ctx: Ctx) -> D:
    r = conn.execute(
        """select coalesce(sum(greatest(s, 0) * custo), 0) v from (
             select p.custo_medio custo, coalesce(sum(m.quantidade), 0) s
             from produtos p left join movimentacoes_estoque m on m.produto_id = p.id
             where p.estabelecimento_id = %s group by p.id) t""", (ctx.estab,)).fetchone()
    return calc.money(r["v"])


# ---------- Produtos ----------
class ProdutoIn(BaseModel):
    nome: str = Field(min_length=1)
    unidade: str = Field(min_length=1)
    custo: D = Field(ge=0)


@router.post("/produtos")
def criar_produto(p: ProdutoIn, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    if conn.execute("select 1 from produtos where estabelecimento_id = %s and nome = %s", (ctx.estab, p.nome)).fetchone():
        raise HTTPException(409, "produto já cadastrado")
    conn.execute("insert into produtos (estabelecimento_id, nome, unidade, custo_medio) values (%s, %s, %s, %s)",
                 (ctx.estab, p.nome, p.unidade, p.custo))
    auditar(conn, ctx, f"cadastrou o produto {p.nome}")
    conn.commit()
    return resp({"nome": p.nome, "unidade": p.unidade, "custo": p.custo}, 201)


@router.get("/produtos")
def listar_produtos(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    rows = conn.execute("select nome, unidade, custo_medio custo from produtos where estabelecimento_id = %s and ativo order by nome", (ctx.estab,)).fetchall()
    return resp(rows)


# ---------- Estoque ----------
class MovimentoIn(BaseModel):
    produto: str
    tipo: Literal["entrada", "saida", "contagem"]
    quantidade: D = Field(ge=0)
    data: date


@router.post("/estoque/movimentacoes")
def movimentar(m: MovimentoIn, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    p = achar(conn, ctx, m.produto)
    atual = saldo(conn, p["id"])
    delta = {"entrada": m.quantidade, "saida": -m.quantidade, "contagem": m.quantidade - atual}[m.tipo]
    if atual + delta < 0:
        raise HTTPException(409, "saída maior que o saldo em estoque")
    mover(conn, ctx, p["id"], delta, m.data, m.tipo, "manual")
    auditar(conn, ctx, f"{m.tipo} de {m.quantidade} em {m.produto}")
    conn.commit()
    return resp({"produto": m.produto, "saldo": saldo(conn, p["id"])}, 201)


@router.get("/estoque")
def estoque(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    rows = conn.execute(
        """select p.nome produto, p.unidade, p.custo_medio custo, coalesce(sum(m.quantidade), 0) saldo
           from produtos p left join movimentacoes_estoque m on m.produto_id = p.id
           where p.estabelecimento_id = %s and p.ativo group by p.id order by p.nome""", (ctx.estab,)).fetchall()
    for r in rows:
        r["valor"] = calc.money(max(r["saldo"], D(0)) * r["custo"])
    return resp({"itens": rows, "valor_total": valor_estoque(conn, ctx)})


# ---------- Compras ----------
class CompraIn(BaseModel):
    fornecedor: str = Field(min_length=1)
    produto: str
    quantidade: D = Field(gt=0)
    custo_unit: D = Field(ge=0)
    data: date


@router.post("/compras")
def comprar(c: CompraIn, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    """Gera entrada no estoque e atualiza o custo médio ponderado do produto."""
    p = achar(conn, ctx, c.produto)
    novo = calc.custo_medio_ponderado(max(saldo(conn, p["id"]), D(0)), p["custo_medio"], c.quantidade, c.custo_unit).quantize(CUSTO4)
    forn = conn.execute("insert into fornecedores (estabelecimento_id, nome) values (%s, %s) on conflict (estabelecimento_id, nome) do update set nome = excluded.nome returning id",
                        (ctx.estab, c.fornecedor)).fetchone()["id"]
    compra = conn.execute("insert into compras (estabelecimento_id, fornecedor_id, data) values (%s, %s, %s) returning id", (ctx.estab, forn, c.data)).fetchone()["id"]
    conn.execute("insert into itens_compra (compra_id, produto_id, quantidade, custo_unit) values (%s, %s, %s, %s)", (compra, p["id"], c.quantidade, c.custo_unit))
    conn.execute("update produtos set custo_medio = %s where id = %s", (novo, p["id"]))
    mover(conn, ctx, p["id"], c.quantidade, c.data, "entrada", "compra")
    auditar(conn, ctx, f"comprou {c.quantidade} de {c.produto}")
    conn.commit()
    return resp({"data": c.data, "fornecedor": c.fornecedor, "produto": c.produto, "quantidade": c.quantidade,
                 "custo_unit": c.custo_unit, "total": calc.money(c.quantidade * c.custo_unit), "novo_custo_medio": novo}, 201)


@router.get("/compras")
def listar_compras(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    rows = conn.execute(
        """select c.data, f.nome fornecedor, p.nome produto, i.quantidade, i.custo_unit, round(i.quantidade * i.custo_unit, 2) total
           from compras c join itens_compra i on i.compra_id = c.id join produtos p on p.id = i.produto_id
           left join fornecedores f on f.id = c.fornecedor_id
           where c.estabelecimento_id = %s order by c.data, c.criado_em""", (ctx.estab,)).fetchall()
    return resp(rows)


# ---------- Fichas técnicas (produtos de venda) ----------
class ItemFicha(BaseModel):
    produto: str
    quantidade: D = Field(gt=0)


class FichaIn(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    categoria: str = Field(default="Sem categoria", min_length=1, max_length=40)
    preco_venda: D = Field(default=D(0), ge=0, le=100000)
    outros_custos: D = Field(default=D(0), ge=0)
    itens: list[ItemFicha] = Field(min_length=1)


def itens_da_ficha(conn, ficha_id) -> list[dict]:
    return conn.execute("select i.produto_id, p.nome produto, i.quantidade, p.custo_medio from itens_ficha i join produtos p on p.id = i.produto_id where i.ficha_id = %s order by p.nome", (ficha_id,)).fetchall()


def custo_da_ficha(itens: list[dict], outros: D) -> D:
    return calc.custo_ficha([(i["quantidade"], i["custo_medio"]) for i in itens], outros)


def preco_efetivo(f: dict) -> D:
    return f["preco_promocional"] or f["preco_venda"]


def achar_ficha(conn, ctx: Ctx, nome: str) -> dict:
    f = conn.execute("select id, nome, categoria, preco_venda, preco_promocional, ativo, outros_custos from fichas_tecnicas where estabelecimento_id = %s and nome = %s", (ctx.estab, nome)).fetchone()
    if f is None:
        raise HTTPException(404, f"ficha não cadastrada: {nome}")
    return f


def meta_cmv(conn, ctx: Ctx) -> D:
    return conn.execute("select meta_cmv_pct from estabelecimentos where id = %s", (ctx.estab,)).fetchone()["meta_cmv_pct"]


def margens(custo: D, preco: D) -> dict:
    if not preco:
        return {"margem_unitaria": None, "margem_pct": None, "cmv_item_pct": None}
    return {"margem_unitaria": calc.money(preco - custo), "margem_pct": calc.cmv_pct(preco - custo, preco), "cmv_item_pct": calc.cmv_pct(custo, preco)}


@router.post("/fichas")
def criar_ficha(f: FichaIn, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    if conn.execute("select 1 from fichas_tecnicas where estabelecimento_id = %s and nome = %s", (ctx.estab, f.nome)).fetchone():
        raise HTTPException(409, "ficha já cadastrada")
    if len({i.produto for i in f.itens}) != len(f.itens):
        raise HTTPException(422, "ingrediente repetido na ficha")
    prods = [achar(conn, ctx, i.produto) for i in f.itens]
    fid = conn.execute("insert into fichas_tecnicas (estabelecimento_id, nome, categoria, preco_venda, outros_custos) values (%s, %s, %s, %s, %s) returning id",
                       (ctx.estab, f.nome, f.categoria, f.preco_venda, f.outros_custos)).fetchone()["id"]
    for i, p in zip(f.itens, prods):
        conn.execute("insert into itens_ficha (ficha_id, produto_id, quantidade) values (%s, %s, %s)", (fid, p["id"], i.quantidade))
    auditar(conn, ctx, f"criou a ficha {f.nome}")
    conn.commit()
    return resp({"nome": f.nome, "categoria": f.categoria, "preco_venda": f.preco_venda, "custo": custo_da_ficha(itens_da_ficha(conn, fid), f.outros_custos)}, 201)


@router.get("/fichas")
def listar_fichas(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    out = []
    for f in conn.execute("select id, nome, categoria, preco_venda, preco_promocional, ativo, outros_custos from fichas_tecnicas where estabelecimento_id = %s order by categoria, nome", (ctx.estab,)).fetchall():
        it = itens_da_ficha(conn, f["id"])
        custo = custo_da_ficha(it, f["outros_custos"])
        out.append({"nome": f["nome"], "categoria": f["categoria"], "ativo": f["ativo"], "custo": custo, "outros_custos": f["outros_custos"],
                    "preco_venda": f["preco_venda"], "preco_promocional": f["preco_promocional"], **margens(custo, preco_efetivo(f)),
                    "itens": [{"produto": i["produto"], "quantidade": i["quantidade"]} for i in it]})
    return resp(out)


class PrecoIn(BaseModel):
    preco_venda: D = Field(gt=0, le=100000)


class PromocaoIn(BaseModel):
    preco_promocional: Optional[D] = Field(default=None, gt=0, le=100000)  # nulo remove a promoção


class DisponibilidadeIn(BaseModel):
    ativo: bool


@router.put("/fichas/{nome}/preco")
def definir_preco(nome: str, b: PrecoIn, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    f = achar_ficha(conn, ctx, nome)
    if f["preco_promocional"] is not None and f["preco_promocional"] >= b.preco_venda:
        raise HTTPException(409, "o preço de venda ficaria menor ou igual ao promocional; remova a promoção antes")
    conn.execute("update fichas_tecnicas set preco_venda = %s where id = %s", (b.preco_venda, f["id"]))
    auditar(conn, ctx, f"definiu o preço de {nome} em {b.preco_venda}")
    conn.commit()
    return resp({"nome": nome, "preco_venda": b.preco_venda})


@router.put("/fichas/{nome}/promocao")
def definir_promocao(nome: str, b: PromocaoIn, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    """Aplica ou remove o preço promocional e já informa o impacto na margem e no CMV do item."""
    f = achar_ficha(conn, ctx, nome)
    if b.preco_promocional is not None and not (f["preco_venda"] and b.preco_promocional < f["preco_venda"]):
        raise HTTPException(422, "o preço promocional deve ser menor que o preço de venda")
    conn.execute("update fichas_tecnicas set preco_promocional = %s where id = %s", (b.preco_promocional, f["id"]))
    custo = custo_da_ficha(itens_da_ficha(conn, f["id"]), f["outros_custos"])
    preco = b.preco_promocional or f["preco_venda"]
    m = margens(custo, preco)
    auditar(conn, ctx, f"{'aplicou' if b.preco_promocional else 'removeu'} a promoção de {nome}")
    conn.commit()
    return resp({"nome": nome, "preco_venda": f["preco_venda"], "preco_promocional": b.preco_promocional, "custo": custo, **m,
                 "acima_da_meta": bool(m["cmv_item_pct"] is not None and m["cmv_item_pct"] > meta_cmv(conn, ctx)),
                 "abaixo_do_custo": bool(preco and preco <= custo)})


@router.put("/fichas/{nome}/disponibilidade")
def definir_disponibilidade(nome: str, b: DisponibilidadeIn, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    f = achar_ficha(conn, ctx, nome)
    conn.execute("update fichas_tecnicas set ativo = %s where id = %s", (b.ativo, f["id"]))
    auditar(conn, ctx, f"marcou {nome} como {'disponível' if b.ativo else 'esgotado'}")
    conn.commit()
    return resp({"nome": nome, "ativo": b.ativo})


# ---------- Vendas ----------
def registrar_venda(conn, ctx: Ctx, f: dict, qtd: D, preco: D, dia: date, pedido_id=None) -> tuple[D, list[str]]:
    """Grava a venda, calcula o custo teórico e dá baixa nos ingredientes. Devolve (custo, ingredientes com estoque negativo)."""
    itens = itens_da_ficha(conn, f["id"])
    custo = calc.money(custo_da_ficha(itens, f["outros_custos"]) * qtd)
    conn.execute("insert into vendas (estabelecimento_id, ficha_id, data, quantidade, preco_unit, custo_teorico, pedido_id) values (%s, %s, %s, %s, %s, %s, %s)",
                 (ctx.estab, f["id"], dia, qtd, preco, custo, pedido_id))
    for i in itens:
        mover(conn, ctx, i["produto_id"], -(i["quantidade"] * qtd), dia, "saida", "venda")
    return custo, [i["produto"] for i in itens if saldo(conn, i["produto_id"]) < 0]


class VendaIn(BaseModel):
    ficha: str
    quantidade: D = Field(gt=0)
    preco_unit: Optional[D] = Field(default=None, gt=0)  # se omitido, usa o preço de venda (ou promocional) da ficha
    data: date


@router.post("/vendas")
def vender(v: VendaIn, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    """Dá baixa automática nos ingredientes da ficha. Estoque negativo é permitido, mas sinalizado."""
    f = achar_ficha(conn, ctx, v.ficha)
    preco = v.preco_unit if v.preco_unit is not None else preco_efetivo(f)
    if not preco:
        raise HTTPException(422, "informe preco_unit ou defina o preço de venda da ficha")
    custo, negativos = registrar_venda(conn, ctx, f, v.quantidade, preco, v.data)
    auditar(conn, ctx, f"vendeu {v.quantidade} × {v.ficha}")
    conn.commit()
    return resp({"data": v.data, "ficha": v.ficha, "quantidade": v.quantidade, "faturamento": calc.money(v.quantidade * preco),
                 "custo_teorico": custo, "estoque_negativo": negativos}, 201)


@router.get("/vendas")
def listar_vendas(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    rows = conn.execute(
        """select v.data, f.nome ficha, v.quantidade, round(v.quantidade * v.preco_unit, 2) faturamento, v.custo_teorico
           from vendas v join fichas_tecnicas f on f.id = v.ficha_id where v.estabelecimento_id = %s order by v.data, v.criado_em""", (ctx.estab,)).fetchall()
    return resp(rows)


# ---------- Perdas ----------
class PerdaIn(BaseModel):
    produto: str
    quantidade: D = Field(gt=0)
    motivo: Literal["vencimento", "descarte", "quebra"]
    data: date


@router.post("/perdas")
def perder(p: PerdaIn, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    prod = achar(conn, ctx, p.produto)
    if saldo(conn, prod["id"]) < p.quantidade:
        raise HTTPException(409, "perda maior que o saldo em estoque")
    valor = calc.money(p.quantidade * prod["custo_medio"])
    conn.execute("insert into perdas (estabelecimento_id, produto_id, data, quantidade, motivo, valor) values (%s, %s, %s, %s, %s, %s)",
                 (ctx.estab, prod["id"], p.data, p.quantidade, p.motivo, valor))
    mover(conn, ctx, prod["id"], -p.quantidade, p.data, "perda", "perda")
    auditar(conn, ctx, f"registrou perda de {p.quantidade} de {p.produto}", p.motivo)
    conn.commit()
    return resp({"data": p.data, "produto": p.produto, "quantidade": p.quantidade, "motivo": p.motivo, "valor": valor}, 201)


@router.get("/perdas")
def listar_perdas(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    rows = conn.execute(
        """select x.data, p.nome produto, x.quantidade, x.motivo, x.valor
           from perdas x join produtos p on p.id = x.produto_id where x.estabelecimento_id = %s order by x.data, x.criado_em""", (ctx.estab,)).fetchall()
    return resp(rows)


# ---------- Sugestão para o fechamento ----------
@router.get("/fechamentos/sugestao")
def sugestao(data: date = Query(...), ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    """Pré-preenche o fechamento com o que foi lançado no dia."""
    q = lambda sql: calc.money(conn.execute(sql, (ctx.estab, data)).fetchone()["v"])
    ant = conn.execute("select estoque_final from fechamentos_diarios where estabelecimento_id = %s and status = 'fechado' and data < %s order by data desc limit 1",
                       (ctx.estab, data)).fetchone()
    return resp({
        "data": data,
        "estoque_inicial": ant["estoque_final"] if ant else None,
        "compras_liquidas": q("select coalesce(sum(i.quantidade * i.custo_unit), 0) v from compras c join itens_compra i on i.compra_id = c.id where c.estabelecimento_id = %s and c.data = %s"),
        "estoque_final": valor_estoque(conn, ctx),
        "faturamento": q("select coalesce(sum(round(quantidade * preco_unit, 2)), 0) v from vendas where estabelecimento_id = %s and data = %s"),
        "custo_teorico": q("select coalesce(sum(custo_teorico), 0) v from vendas where estabelecimento_id = %s and data = %s"),
        "perdas": q("select coalesce(sum(valor), 0) v from perdas where estabelecimento_id = %s and data = %s"),
    })
