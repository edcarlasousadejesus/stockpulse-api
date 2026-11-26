"""Recursos de IA e controle de uso/planos SaaS (geração de arte promocional e quotas por estabelecimento)."""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import date
from decimal import Decimal
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .core import Ctx, auditar, contexto, get_conn, hoje, resp

log = logging.getLogger("stockpulse.ia")
router = APIRouter(prefix="/ia", tags=["ia"])

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
IA_IMAGE_PROVIDER = os.environ.get("IA_IMAGE_PROVIDER", "auto").lower()

# Limites diários padrão por plano (podem ser sobrescritos por variáveis de ambiente)
LIMITES_PLANO = {
    "free": {"gerar_arte": int(os.environ.get("IA_LIMITE_ARTES_FREE", "3")), "whatsapp_tts": 20},
    "starter": {"gerar_arte": int(os.environ.get("IA_LIMITE_ARTES_STARTER", "15")), "whatsapp_tts": 150},
    "pro": {"gerar_arte": int(os.environ.get("IA_LIMITE_ARTES_PRO", "60")), "whatsapp_tts": 1000},
    "enterprise": {"gerar_arte": 9999, "whatsapp_tts": 99999},
}

# Fallback em memória quando a tabela uso_ia ainda não foi migrada
_USO_MEM: dict[tuple[str, str, str], int] = {}


def obter_plano_estab(conn, estab_id: str) -> tuple[str, bool]:
    """Obtém plano e flag ia_ativo com fallback seguro caso a migração 004 ainda não tenha sido aplicada."""
    try:
        row = conn.execute("select plano, ia_ativo from estabelecimentos where id = %s", (estab_id,)).fetchone()
        if row:
            return (row.get("plano") or "starter"), bool(row.get("ia_ativo", True))
    except Exception:
        pass
    return "starter", True


def contar_uso_dia(conn, estab_id: str, recurso: str, dia: Optional[date] = None) -> int:
    d = dia or hoje()
    try:
        row = conn.execute(
            "select coalesce(sum(custo_unidades), 0) total from uso_ia where estabelecimento_id = %s and recurso = %s and criado_em::date = %s",
            (estab_id, recurso, d),
        ).fetchone()
        if row is not None and "total" in row:
            return int(row["total"] or 0)
    except Exception:
        pass
    return _USO_MEM.get((estab_id, recurso, d.isoformat()), 0)


def registrar_uso_ia(conn, estab_id: str, uid: Optional[str], recurso: str, provedor: str, metadados: Optional[dict] = None) -> None:
    d = hoje().isoformat()
    _USO_MEM[(estab_id, recurso, d)] = _USO_MEM.get((estab_id, recurso, d), 0) + 1
    try:
        conn.execute(
            "insert into uso_ia (estabelecimento_id, usuario_id, recurso, provedor, metadados) values (%s, %s, %s, %s, %s::jsonb)",
            (estab_id, uid, recurso, provedor, json.dumps(metadados or {}, ensure_ascii=False)),
        )
    except Exception:
        # Não bloqueia a operação se a tabela uso_ia ainda não existir no banco
        pass


def verificar_cota_ia(conn, estab_id: str, recurso: str) -> dict:
    plano, ativo = obter_plano_estab(conn, estab_id)
    if not ativo:
        raise HTTPException(403, "recursos de IA desativados para este estabelecimento")
    limites = LIMITES_PLANO.get(plano, LIMITES_PLANO["starter"])
    limite = limites.get(recurso, 20)
    usado = contar_uso_dia(conn, estab_id, recurso)
    if usado >= limite:
        raise HTTPException(429, f"limite diário do plano '{plano}' atingido para {recurso} ({usado}/{limite})")
    return {"plano": plano, "usado_hoje": usado, "limite_dia": limite, "restante": max(0, limite - usado)}


class IngredienteArteIn(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    quantidade: Optional[Decimal] = None


class MarcaArteIn(BaseModel):
    nome: str = Field(default="", max_length=80)
    slogan: str = Field(default="", max_length=120)
    zap: str = Field(default="", max_length=30)
    cor: str = Field(default="#7fee64", max_length=20)
    logo: Optional[str] = None


class GerarArteIn(BaseModel):
    produto: str = Field(min_length=1, max_length=100)
    categoria: str = Field(default="Cardápio", max_length=80)
    ingredientes: list[IngredienteArteIn] = Field(default_factory=list, max_length=40)
    preco_normal: Decimal = Field(gt=0)
    preco_promocional: Decimal = Field(gt=0)
    desconto_pct: Optional[int] = Field(default=None, ge=1, le=99)
    titulo: str = Field(default="Promoção", max_length=80)
    validade: str = Field(default="Por tempo limitado", max_length=80)
    marca: MarcaArteIn = Field(default_factory=MarcaArteIn)


def _montar_prompt_arte(b: GerarArteIn) -> str:
    ings = ", ".join(i.nome for i in b.ingredientes[:10]) or "ingredientes selecionados"
    off = b.desconto_pct or max(1, round((1 - float(b.preco_promocional) / float(b.preco_normal)) * 100))
    marca_nome = b.marca.nome or "Restaurante"
    cor = b.marca.cor or "#7fee64"
    return (
        f"Create a square 1:1 high-converting dark-mode social media promotional food poster for '{marca_nome}'. "
        f"Featured dish: '{b.produto}' (category: {b.categoria}), made with {ings}. "
        f"Visual style: sleek dark background (#0a0a0a to #181818) with vibrant accent glow in {cor}, "
        f"appetizing studio food photography of {b.produto} as the centerpiece, crisp modern typography. "
        f"Headline text: '{b.titulo.upper()}'. "
        f"Price callout: from R$ {b.preco_normal:.2f} for R$ {b.preco_promocional:.2f} (-{off}% OFF). "
        f"Footer text: '{b.validade}'"
        + (f" · WhatsApp: {b.marca.zap}" if b.marca.zap else "")
        + "."
    )


def gerar_imagem_ia(prompt: str) -> tuple[str, str]:
    """Gera imagem promocional via Gemini ou OpenAI Images. Retorna (base64_png, provedor)."""
    use_openai = IA_IMAGE_PROVIDER == "openai" or (IA_IMAGE_PROVIDER == "auto" and OPENAI_KEY and not GEMINI_KEY)

    if use_openai and OPENAI_KEY:
        try:
            r = httpx.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
                json={"model": os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1"), "prompt": prompt, "size": "1024x1024"},
                timeout=45,
            )
            r.raise_for_status()
            data = r.json().get("data") or []
            if data and data[0].get("b64_json"):
                return data[0]["b64_json"], "openai"
            if data and data[0].get("url"):
                img = httpx.get(data[0]["url"], timeout=20)
                img.raise_for_status()
                return base64.b64encode(img.content).decode(), "openai"
        except Exception as e:
            log.warning("Falha ao gerar imagem via OpenAI: %s", e)
            if not GEMINI_KEY:
                raise HTTPException(502, "falha ao gerar arte com OpenAI")

    if GEMINI_KEY:
        model = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.0-flash-exp-image-generation")
        try:
            r = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}",
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
                },
                timeout=45,
            )
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
            for part in parts:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return inline["data"], "gemini"
        except Exception as e:
            log.warning("Falha ao gerar imagem via Gemini: %s", e)
            raise HTTPException(502, "falha ao gerar arte com Gemini")

    raise HTTPException(503, "serviço de geração de imagem com IA não configurado (defina GEMINI_API_KEY ou OPENAI_API_KEY)")


@router.get("/uso")
def consultar_uso(ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    """Consulta o plano atual e a cota diária de IA do estabelecimento."""
    plano, ativo = obter_plano_estab(conn, ctx.estab)
    limites = LIMITES_PLANO.get(plano, LIMITES_PLANO["starter"])
    return resp({
        "estabelecimento_id": ctx.estab,
        "plano": plano,
        "ia_ativo": ativo,
        "uso_hoje": {
            "gerar_arte": contar_uso_dia(conn, ctx.estab, "gerar_arte"),
            "whatsapp_tts": contar_uso_dia(conn, ctx.estab, "whatsapp_tts"),
        },
        "limites_dia": limites,
    })


@router.post("/gerar-arte")
def endpoint_gerar_arte(b: GerarArteIn, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    """Gera arte promocional com IA a partir da ficha técnica e identidade da marca."""
    if b.preco_promocional >= b.preco_normal:
        raise HTTPException(422, "o preço promocional deve ser menor que o preço normal")
    cota = verificar_cota_ia(conn, ctx.estab, "gerar_arte")
    prompt = _montar_prompt_arte(b)
    b64, provedor = gerar_imagem_ia(prompt)
    registrar_uso_ia(conn, ctx.estab, ctx.uid, "gerar_arte", provedor, {"produto": b.produto})
    auditar(conn, ctx, f"gerou arte promocional com IA ({provedor}) para {b.produto}")
    conn.commit()
    return resp({
        "imagem_base64": b64,
        "mime_type": "image/png",
        "provedor": provedor,
        "uso": {**cota, "usado_hoje": cota["usado_hoje"] + 1, "restante": max(0, cota["restante"] - 1)},
    })
