"""Cardápio público de cada restaurante: SEM login, só o que o cliente final pode ver.
Nunca devolve custo, margem, estoque nem identificadores internos. Os preços sempre vêm do banco."""
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .core import get_conn, ip_do_cliente, resp
from .pedidos import ItemPedidoIn, criar_pedido, telefone

router = APIRouter(prefix="/publico")
_TENTATIVAS: dict = {}
MAX_PEDIDOS, JANELA = 10, 3600  # por IP e por restaurante, a cada hora


def limitar(chave) -> None:
    agora = time.time()
    if len(_TENTATIVAS) > 20000:
        _TENTATIVAS.clear()
    recentes = [t for t in _TENTATIVAS.get(chave, []) if agora - t < JANELA]
    if len(recentes) >= MAX_PEDIDOS:
        raise HTTPException(429, "muitos pedidos em pouco tempo; tente novamente mais tarde")
    _TENTATIVAS[chave] = recentes + [agora]


def _loja(conn, slug: str) -> dict:
    e = conn.execute("select id, nome, marca_nome, slogan, whatsapp, cor_marca, logo_url from estabelecimentos where slug = %s and cardapio_publico", (slug,)).fetchone()
    if e is None:
        raise HTTPException(404, "cardápio não encontrado")
    return e


@router.get("/{slug}/cardapio")
def cardapio(slug: str, conn=Depends(get_conn)):
    e = _loja(conn, slug)
    rows = conn.execute("select categoria, nome, preco_venda, preco_promocional from fichas_tecnicas where estabelecimento_id = %s and ativo and preco_venda > 0 order by categoria, nome", (e["id"],)).fetchall()
    cats: dict = {}
    for r in rows:
        promo = r["preco_promocional"]
        cats.setdefault(r["categoria"], []).append({"nome": r["nome"], "preco": promo or r["preco_venda"], "preco_original": r["preco_venda"] if promo else None})
    return resp({"nome": e["marca_nome"] or e["nome"], "slogan": e["slogan"], "whatsapp": e["whatsapp"], "cor": e["cor_marca"], "logo_url": e["logo_url"],
                 "categorias": [{"nome": c, "itens": i} for c, i in cats.items()]})


class PedidoPublicoIn(BaseModel):
    cliente_nome: str = Field(min_length=1, max_length=80)
    cliente_telefone: Optional[str] = None
    observacao: Optional[str] = Field(default=None, max_length=300)
    itens: list[ItemPedidoIn] = Field(min_length=1, max_length=30)

    _tel = field_validator("cliente_telefone")(lambda cls, v: telefone(v))


@router.post("/{slug}/pedidos")
def fazer_pedido(slug: str, p: PedidoPublicoIn, req: Request, conn=Depends(get_conn)):
    e = _loja(conn, slug)
    limitar((ip_do_cliente(req), slug))
    r = criar_pedido(conn, e["id"], p.cliente_nome, p.cliente_telefone, "Cardápio", "A combinar", p.observacao, p.itens)
    conn.execute("insert into auditoria (estabelecimento_id, usuario_nome, perfil, acao) values (%s, 'cardápio', 'cliente', %s)", (e["id"], f"pedido #{r['numero']} recebido pelo cardápio"))
    conn.commit()
    return resp({"numero": r["numero"], "total": r["total"], "mensagem": "Pedido recebido! Em instantes o restaurante confirma."}, 201)
