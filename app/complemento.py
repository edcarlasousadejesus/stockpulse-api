"""Complemento StockPulse: Assistente IA, Bloqueios, Cross-sell, Recuperação, Entrega/Motoboys e CRM."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .core import Ctx, auditar, contexto, get_conn, resp, so_gestao
from .pedidos import telefone
from .publico import itens_cardapio

router = APIRouter(tags=["complemento"])

# Fallbacks em memória para testes sem banco ou antes da migração 005
_ASSISTENTE_MEM: dict[str, dict] = {}
_BLOQUEIOS_MEM: dict[str, set[str]] = {}
_CARRINHOS_MEM: dict[str, list[dict]] = {}
_ENTREGA_MEM: dict[str, dict] = {}
_MOTOBOYS_MEM: dict[str, list[dict]] = {}
_CUPONS_MEM: dict[str, list[dict]] = {}
_FIDELIDADE_MEM: dict[tuple[str, str], dict] = {}

DEFAULT_ASSISTENTE = {
    "nome": "Atendente StockPulse",
    "voz_tts": "nova",
    "formalidade": "equilibrado",
    "estilo": "Simpático, objetivo e focado em ajudar o cliente a fechar o pedido.",
    "tempo_recuperacao_min": 15,
}


def _norm_tel(v: Optional[str]) -> str:
    return re.sub(r"\D", "", str(v or ""))


# ---------------------------------------------------------------------------
# 1) Personalização da Assistente IA & Lista de Bloqueio (WhatsApp)
# ---------------------------------------------------------------------------

def obter_assistente_estab(conn, estab_id: str) -> dict:
    try:
        row = conn.execute("select assistente_ia from estabelecimentos where id = %s", (estab_id,)).fetchone()
        if row and isinstance(row.get("assistente_ia"), dict):
            return {**DEFAULT_ASSISTENTE, **row["assistente_ia"]}
    except Exception:
        pass
    return {**DEFAULT_ASSISTENTE, **_ASSISTENTE_MEM.get(estab_id, {})}


def obter_bloqueios_estab(conn, estab_id: str) -> list[str]:
    try:
        row = conn.execute("select whatsapp_bloqueios from estabelecimentos where id = %s", (estab_id,)).fetchone()
        if row and isinstance(row.get("whatsapp_bloqueios"), list):
            return [str(x) for x in row["whatsapp_bloqueios"]]
    except Exception:
        pass
    return sorted(_BLOQUEIOS_MEM.get(estab_id, set()))


def esta_bloqueado(conn, estab_id: str, tel: Optional[str]) -> bool:
    d = _norm_tel(tel)
    if not d:
        return False
    bloqueados = {_norm_tel(x) for x in obter_bloqueios_estab(conn, estab_id)}
    return d in bloqueados or (len(d) >= 11 and d[-11:] in {b[-11:] for b in bloqueados if len(b) >= 11})


class AssistenteIn(BaseModel):
    nome: str = Field(default="Atendente StockPulse", min_length=1, max_length=60)
    voz_tts: Literal["nova", "alloy", "echo", "fable", "onyx", "shimmer"] = "nova"
    formalidade: Literal["informal", "equilibrado", "formal"] = "equilibrado"
    estilo: str = Field(default="", max_length=300)
    tempo_recuperacao_min: int = Field(default=15, ge=1, le=1440)


class BloqueioIn(BaseModel):
    telefone: str = Field(min_length=8, max_length=25)


@router.get("/ia/assistente")
def get_assistente(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    return resp({
        "assistente": obter_assistente_estab(conn, ctx.estab),
        "bloqueios": obter_bloqueios_estab(conn, ctx.estab),
    })


@router.put("/ia/assistente")
def put_assistente(b: AssistenteIn, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    dados = b.model_dump()
    _ASSISTENTE_MEM[ctx.estab] = dados
    try:
        conn.execute("update estabelecimentos set assistente_ia = %s::jsonb where id = %s", (json.dumps(dados, ensure_ascii=False), ctx.estab))
    except Exception:
        pass
    auditar(conn, ctx, "atualizou personalização da assistente IA")
    conn.commit()
    return resp(dados)


@router.get("/ia/bloqueios")
def get_bloqueios(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    return resp(obter_bloqueios_estab(conn, ctx.estab))


@router.post("/ia/bloqueios", status_code=201)
def add_bloqueio(b: BloqueioIn, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    d = _norm_tel(b.telefone)
    if len(d) < 8:
        raise HTTPException(422, "telefone inválido para bloqueio")
    atuais = set(obter_bloqueios_estab(conn, ctx.estab))
    atuais.add(d)
    lista = sorted(atuais)
    _BLOQUEIOS_MEM[ctx.estab] = atuais
    try:
        conn.execute("update estabelecimentos set whatsapp_bloqueios = %s::jsonb where id = %s", (json.dumps(lista), ctx.estab))
    except Exception:
        pass
    auditar(conn, ctx, f"bloqueou contato WhatsApp {d}")
    conn.commit()
    return resp(lista, 201)


@router.delete("/ia/bloqueios/{tel}")
def del_bloqueio(tel: str, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    d = _norm_tel(tel)
    atuais = set(obter_bloqueios_estab(conn, ctx.estab))
    atuais.discard(d)
    lista = sorted(atuais)
    _BLOQUEIOS_MEM[ctx.estab] = atuais
    try:
        conn.execute("update estabelecimentos set whatsapp_bloqueios = %s::jsonb where id = %s", (json.dumps(lista), ctx.estab))
    except Exception:
        pass
    auditar(conn, ctx, f"desbloqueou contato WhatsApp {d}")
    conn.commit()
    return resp(lista)


# ---------------------------------------------------------------------------
# 2) Cross-sell Inteligente & Recuperação de Carrinho/Conversa
# ---------------------------------------------------------------------------

def sugerir_cross_sell(conn, estab_id: str, itens_atuais: list[str], limite: int = 3) -> list[dict]:
    """Sugere acompanhamentos, bebidas ou sobremesas com base nos últimos 90 dias ou categorias ativas."""
    atuais_l = {i.strip().lower() for i in itens_atuais if i and i.strip()}
    cats = itens_cardapio(conn, estab_id, filtrar_estoque=True)
    disponiveis = {it["nome"]: {**it, "categoria": c.get("nome") or c.get("categoria") or "Cardápio"} for c in cats for it in c["itens"]}
    if not disponiveis:
        return []

    sugestoes: list[dict] = []
    usados = set(atuais_l)

    # 1) Co-ocorrência real nos últimos 90 dias (itens_pedido)
    if atuais_l:
        try:
            rows = conn.execute(
                """select i2.nome, count(*) freq
                   from itens_pedido i1
                   join itens_pedido i2 on i2.pedido_id = i1.pedido_id and i2.nome <> i1.nome
                   join pedidos p on p.id = i1.pedido_id
                   where p.estabelecimento_id = %s
                     and p.status <> 'cancelado'
                     and p.criado_em >= now() - interval '90 days'
                     and lower(i1.nome) = any(%s)
                   group by i2.nome
                   order by freq desc
                   limit 10""",
                (estab_id, list(atuais_l)),
            ).fetchall()
            for r in rows:
                n = r["nome"]
                if n.lower() not in usados and n in disponiveis:
                    sugestoes.append({**disponiveis[n], "motivo": "Mais pedido junto nos últimos 90 dias"})
                    usados.add(n.lower())
                    if len(sugestoes) >= limite:
                        return sugestoes
        except Exception:
            pass

    # 2) Fallback / complemento por categorias típicas (Bebidas, Sobremesas, Acompanhamentos)
    prioridade = ("bebida", "sobremesa", "doce", "acompanhamento", "porç", "porc", "combo")
    ordenados = sorted(
        disponiveis.values(),
        key=lambda x: (0 if any(p in (x.get("categoria") or "").lower() for p in prioridade) else 1, x["nome"]),
    )
    for it in ordenados:
        if it["nome"].lower() not in usados:
            sugestoes.append({**it, "motivo": f"Sugestão de {it.get('categoria') or 'acompanhamento'}"})
            usados.add(it["nome"].lower())
            if len(sugestoes) >= limite:
                break
    return sugestoes


@router.get("/ia/cross-sell")
def get_cross_sell(itens: str = Query(default=""), ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    lista = [x.strip() for x in itens.split(",") if x.strip()]
    return resp({"sugestoes": sugerir_cross_sell(conn, ctx.estab, lista)})


class CarrinhoIn(BaseModel):
    telefone: str = Field(min_length=8, max_length=25)
    cliente_nome: str = Field(default="Cliente", max_length=80)
    itens: list[dict] = Field(min_length=1)
    total: Decimal = Field(ge=0)
    canal: str = Field(default="WhatsApp", max_length=30)


def registrar_carrinho_aberto(conn, estab_id: str, tel: Optional[str], nome: str, itens: list[dict], total: Decimal, canal: str = "WhatsApp") -> None:
    d = _norm_tel(tel)
    if not d or not itens:
        return
    arr = _CARRINHOS_MEM.setdefault(estab_id, [])
    arr[:] = [c for c in arr if not (c["telefone"] == d and c["status"] == "aberto")]
    arr.append({
        "id": str(uuid.uuid4()),
        "telefone": d,
        "cliente_nome": nome,
        "itens": itens,
        "total": str(total),
        "canal": canal,
        "status": "aberto",
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
    })
    try:
        conn.execute("delete from carrinhos_abandonados where estabelecimento_id = %s and telefone = %s and status = 'aberto'", (estab_id, d))
        conn.execute(
            "insert into carrinhos_abandonados (estabelecimento_id, telefone, cliente_nome, itens, total, canal) values (%s, %s, %s, %s::jsonb, %s, %s)",
            (estab_id, d, nome, json.dumps(itens, ensure_ascii=False), total, canal),
        )
    except Exception:
        pass


def marcar_carrinho_recuperado(conn, estab_id: str, tel: Optional[str]) -> None:
    d = _norm_tel(tel)
    if not d:
        return
    for c in _CARRINHOS_MEM.get(estab_id, []):
        if c["telefone"] == d and c["status"] in ("aberto", "lembrado"):
            c["status"] = "recuperado"
    try:
        conn.execute(
            "update carrinhos_abandonados set status = 'recuperado', atualizado_em = now() where estabelecimento_id = %s and telefone = %s and status in ('aberto','lembrado')",
            (estab_id, d),
        )
    except Exception:
        pass


@router.get("/ia/carrinhos")
def listar_carrinhos(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    try:
        rows = conn.execute(
            "select id::text, telefone, cliente_nome, itens, total, canal, status, atualizado_em, lembrado_em from carrinhos_abandonados where estabelecimento_id = %s order by atualizado_em desc limit 50",
            (ctx.estab,),
        ).fetchall()
        return resp(rows)
    except Exception:
        return resp(_CARRINHOS_MEM.get(ctx.estab, []))


@router.post("/ia/carrinhos", status_code=201)
def criar_carrinho(b: CarrinhoIn, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    registrar_carrinho_aberto(conn, ctx.estab, b.telefone, b.cliente_nome, b.itens, b.total, b.canal)
    conn.commit()
    return resp({"ok": True}, 201)


@router.post("/ia/carrinhos/recuperar")
def disparar_recuperacao(minutos: Optional[int] = Query(default=None, ge=0, le=1440), ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    """Dispara lembrete automático no WhatsApp para carrinhos abertos acima do tempo configurado (respeitando bloqueios)."""
    from .whatsapp import enviar_whatsapp

    cfg = obter_assistente_estab(conn, ctx.estab)
    espera = cfg["tempo_recuperacao_min"] if minutos is None else minutos
    corte = datetime.now(timezone.utc) - timedelta(minutes=espera)

    abertos = []
    try:
        abertos = conn.execute(
            "select id::text, telefone, cliente_nome, itens, total, atualizado_em from carrinhos_abandonados where estabelecimento_id = %s and status = 'aberto' and atualizado_em <= %s",
            (ctx.estab, corte),
        ).fetchall()
    except Exception:
        pass
    if not abertos:
        for c in _CARRINHOS_MEM.get(ctx.estab, []):
            if c["status"] == "aberto" and datetime.fromisoformat(c["atualizado_em"]) <= corte:
                abertos.append(c)

    enviados = []
    for c in abertos:
        if esta_bloqueado(conn, ctx.estab, c["telefone"]):
            continue
        msg = (
            f"Oi, {c['cliente_nome'].split()[0]}! Aqui é {cfg['nome']}. "
            f"Vi que você iniciou um pedido conosco (R$ {c['total']}). Quer que eu finalize pra você agora?"
        )
        enviar_whatsapp(c["telefone"], msg)
        enviados.append({"telefone": c["telefone"], "mensagem": msg})
        c["status"] = "lembrado"
        try:
            conn.execute("update carrinhos_abandonados set status = 'lembrado', lembrado_em = now() where id = %s", (c["id"],))
        except Exception:
            pass
    conn.commit()
    return resp({"lembretes_enviados": len(enviados), "detalhes": enviados})


# ---------------------------------------------------------------------------
# 3) Operação de Entrega (Áreas/Taxas + Motoboys)
# ---------------------------------------------------------------------------

class BairroTaxaIn(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    taxa: Decimal = Field(ge=0)


class RaioTaxaIn(BaseModel):
    ate_km: Decimal = Field(gt=0)
    taxa: Decimal = Field(ge=0)


class EntregaConfigIn(BaseModel):
    bairros: list[BairroTaxaIn] = Field(default_factory=list, max_length=100)
    raios: list[RaioTaxaIn] = Field(default_factory=list, max_length=50)


class MotoboyIn(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    telefone: str = Field(min_length=8, max_length=25)
    veiculo: str = Field(default="Moto", max_length=40)


class DespachoIn(BaseModel):
    motoboy_id: Optional[str] = None
    motoboy_nome: Optional[str] = None
    motoboy_telefone: Optional[str] = None
    endereco: str = Field(default="", max_length=200)
    taxa_entrega: Decimal = Field(default=Decimal("0"), ge=0)


@router.get("/entrega/config")
def get_entrega_config(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    try:
        row = conn.execute("select entrega_config from estabelecimentos where id = %s", (ctx.estab,)).fetchone()
        if row and isinstance(row.get("entrega_config"), dict):
            return resp(row["entrega_config"])
    except Exception:
        pass
    return resp(_ENTREGA_MEM.get(ctx.estab, {"bairros": [], "raios": []}))


@router.put("/entrega/config")
def put_entrega_config(b: EntregaConfigIn, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    dados = b.model_dump(mode="json")
    _ENTREGA_MEM[ctx.estab] = dados
    try:
        conn.execute("update estabelecimentos set entrega_config = %s::jsonb where id = %s", (json.dumps(dados, ensure_ascii=False), ctx.estab))
    except Exception:
        pass
    auditar(conn, ctx, "atualizou áreas e taxas de entrega")
    conn.commit()
    return resp(dados)


@router.get("/entrega/motoboys")
def listar_motoboys(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    try:
        rows = conn.execute("select id::text, nome, telefone, veiculo, ativo from motoboys where estabelecimento_id = %s and ativo order by nome", (ctx.estab,)).fetchall()
        return resp(rows)
    except Exception:
        return resp([m for m in _MOTOBOYS_MEM.get(ctx.estab, []) if m.get("ativo", True)])


@router.post("/entrega/motoboys", status_code=201)
def criar_motoboy(b: MotoboyIn, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    tel = telefone(b.telefone)
    mid = str(uuid.uuid4())
    novo = {"id": mid, "nome": b.nome.strip(), "telefone": tel, "veiculo": b.veiculo.strip(), "ativo": True}
    _MOTOBOYS_MEM.setdefault(ctx.estab, []).append(novo)
    try:
        row = conn.execute(
            "insert into motoboys (estabelecimento_id, nome, telefone, veiculo) values (%s, %s, %s, %s) returning id::text",
            (ctx.estab, novo["nome"], tel, novo["veiculo"]),
        ).fetchone()
        if row:
            novo["id"] = row["id"]
    except Exception:
        pass
    auditar(conn, ctx, f"cadastrou motoboy {novo['nome']}")
    conn.commit()
    return resp(novo, 201)


@router.delete("/entrega/motoboys/{mid}")
def remover_motoboy(mid: str, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    _MOTOBOYS_MEM[ctx.estab] = [m for m in _MOTOBOYS_MEM.get(ctx.estab, []) if m["id"] != mid]
    try:
        conn.execute("update motoboys set ativo = false where estabelecimento_id = %s and id = %s", (ctx.estab, mid))
    except Exception:
        pass
    conn.commit()
    return resp({"removido": mid})


@router.post("/pedidos/{numero}/despachar")
def despachar_pedido(numero: int, b: DespachoIn, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    """Envia resumo do pedido + link GPS (Google Maps) para o WhatsApp do motoboy ativo."""
    from .whatsapp import enviar_whatsapp

    tel_moto = b.motoboy_telefone
    nome_moto = b.motoboy_nome or "Motoboy"
    if b.motoboy_id and not tel_moto:
        try:
            m = conn.execute("select nome, telefone from motoboys where estabelecimento_id = %s and id = %s", (ctx.estab, b.motoboy_id)).fetchone()
            if m:
                nome_moto, tel_moto = m["nome"], m["telefone"]
        except Exception:
            pass
        if not tel_moto:
            for m in _MOTOBOYS_MEM.get(ctx.estab, []):
                if m["id"] == b.motoboy_id:
                    nome_moto, tel_moto = m["nome"], m["telefone"]
                    break
    if not tel_moto:
        raise HTTPException(422, "informe um motoboy ativo com telefone")

    p = conn.execute("select cliente_nome, cliente_telefone, total, pagamento, observacao from pedidos where estabelecimento_id = %s and numero = %s", (ctx.estab, numero)).fetchone()
    endereco = b.endereco.strip() or (p.get("observacao") if p else "") or "Consultar cliente"
    gps_link = f"https://maps.google.com/?q={quote(endereco)}"
    msg = (
        f"🛵 *Entrega #{numero}* ({nome_moto})\n"
        f"Cliente: {(p or {}).get('cliente_nome', 'Cliente')} · Tel: {(p or {}).get('cliente_telefone', '-')}\n"
        f"Total: R$ {(p or {}).get('total', '0.00')} ({(p or {}).get('pagamento', 'A combinar')})\n"
        f"Endereço: {endereco}\n"
        f"📍 GPS: {gps_link}"
    )
    enviar_whatsapp(_norm_tel(tel_moto), msg)
    auditar(conn, ctx, f"despachou pedido #{numero} com {nome_moto}")
    conn.commit()
    return resp({"numero": numero, "motoboy": nome_moto, "telefone_motoboy": _norm_tel(tel_moto), "gps_link": gps_link, "mensagem": msg})


# ---------------------------------------------------------------------------
# 4) Base de CRM (Cupons e Fidelidade/Cashback)
# ---------------------------------------------------------------------------

class CupomIn(BaseModel):
    codigo: str = Field(min_length=2, max_length=30)
    tipo: Literal["pct", "fixo"] = "pct"
    valor: Decimal = Field(gt=0)
    limite_uso: int = Field(default=100, ge=1, le=100000)


class ValidarCupomIn(BaseModel):
    codigo: str = Field(min_length=2, max_length=30)
    total: Decimal = Field(gt=0)


class CreditarFidelidadeIn(BaseModel):
    telefone: str = Field(min_length=8, max_length=25)
    valor_pedido: Decimal = Field(gt=0)


def creditar_fidelidade(conn, estab_id: str, tel: Optional[str], valor_pedido: Decimal) -> Optional[dict]:
    d = _norm_tel(tel)
    if not d or valor_pedido <= 0:
        return None
    pontos = max(1, int(valor_pedido // Decimal("10")))
    cashback = (valor_pedido * Decimal("0.05")).quantize(Decimal("0.01"))
    rec = _FIDELIDADE_MEM.setdefault((estab_id, d), {"telefone": d, "pontos": 0, "cashback_saldo": Decimal("0.00")})
    rec["pontos"] += pontos
    rec["cashback_saldo"] += cashback
    try:
        conn.execute(
            """insert into fidelidade_clientes (estabelecimento_id, telefone, pontos, cashback_saldo)
               values (%s, %s, %s, %s)
               on conflict (estabelecimento_id, telefone)
               do update set pontos = fidelidade_clientes.pontos + excluded.pontos,
                             cashback_saldo = fidelidade_clientes.cashback_saldo + excluded.cashback_saldo,
                             atualizado_em = now()""",
            (estab_id, d, pontos, cashback),
        )
    except Exception:
        pass
    return {"telefone": d, "pontos_ganhos": pontos, "cashback_ganho": cashback, "pontos_total": rec["pontos"], "cashback_saldo": rec["cashback_saldo"]}


@router.get("/crm/cupons")
def listar_cupons(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    try:
        rows = conn.execute("select id::text, codigo, tipo, valor, limite_uso, usos, ativo from cupons where estabelecimento_id = %s order by criado_em desc", (ctx.estab,)).fetchall()
        return resp(rows)
    except Exception:
        return resp(_CUPONS_MEM.get(ctx.estab, []))


@router.post("/crm/cupons", status_code=201)
def criar_cupom(b: CupomIn, ctx: Ctx = Depends(so_gestao), conn=Depends(get_conn)):
    cod = b.codigo.strip().upper()
    if b.tipo == "pct" and b.valor > 100:
        raise HTTPException(422, "cupom em % não pode passar de 100")
    novo = {"id": str(uuid.uuid4()), "codigo": cod, "tipo": b.tipo, "valor": b.valor, "limite_uso": b.limite_uso, "usos": 0, "ativo": True}
    arr = _CUPONS_MEM.setdefault(ctx.estab, [])
    arr[:] = [c for c in arr if c["codigo"] != cod]
    arr.insert(0, novo)
    try:
        conn.execute(
            """insert into cupons (estabelecimento_id, codigo, tipo, valor, limite_uso)
               values (%s, %s, %s, %s, %s)
               on conflict (estabelecimento_id, codigo)
               do update set tipo = excluded.tipo, valor = excluded.valor, limite_uso = excluded.limite_uso, ativo = true""",
            (ctx.estab, cod, b.tipo, b.valor, b.limite_uso),
        )
    except Exception:
        pass
    auditar(conn, ctx, f"criou cupom {cod}")
    conn.commit()
    return resp(novo, 201)


@router.post("/crm/cupons/validar")
def validar_cupom(b: ValidarCupomIn, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    cod = b.codigo.strip().upper()
    cupom = None
    try:
        cupom = conn.execute("select id::text, codigo, tipo, valor, limite_uso, usos, ativo from cupons where estabelecimento_id = %s and codigo = %s", (ctx.estab, cod)).fetchone()
    except Exception:
        pass
    if cupom is None:
        cupom = next((c for c in _CUPONS_MEM.get(ctx.estab, []) if c["codigo"] == cod), None)
    if not cupom or not cupom.get("ativo", True) or cupom["usos"] >= cupom["limite_uso"]:
        raise HTTPException(404, "cupom inválido ou esgotado")
    val = Decimal(str(cupom["valor"]))
    desc = (b.total * val / Decimal("100")).quantize(Decimal("0.01")) if cupom["tipo"] == "pct" else min(b.total, val)
    return resp({"codigo": cod, "desconto": desc, "total_com_desconto": max(Decimal("0.00"), b.total - desc)})


@router.get("/crm/fidelidade")
def listar_fidelidade(telefone_cli: Optional[str] = Query(default=None, alias="telefone"), ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    d = _norm_tel(telefone_cli)
    try:
        if d:
            row = conn.execute("select telefone, pontos, cashback_saldo from fidelidade_clientes where estabelecimento_id = %s and telefone = %s", (ctx.estab, d)).fetchone()
            return resp(row or {"telefone": d, "pontos": 0, "cashback_saldo": Decimal("0.00")})
        rows = conn.execute("select telefone, pontos, cashback_saldo from fidelidade_clientes where estabelecimento_id = %s order by pontos desc limit 100", (ctx.estab,)).fetchall()
        return resp(rows)
    except Exception:
        if d:
            return resp(_FIDELIDADE_MEM.get((ctx.estab, d), {"telefone": d, "pontos": 0, "cashback_saldo": Decimal("0.00")}))
        return resp([v for (e, _), v in _FIDELIDADE_MEM.items() if e == ctx.estab])


@router.post("/crm/fidelidade/creditar")
def endpoint_creditar_fidelidade(b: CreditarFidelidadeIn, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    res = creditar_fidelidade(conn, ctx.estab, b.telefone, b.valor_pedido)
    conn.commit()
    return resp(res)
