"""
StockPulse AI — Chatbot conversacional híbrido.

Arquitetura:
  Interface → Interpretação (NLU) → Orquestrador → Dados → Geração de resposta

Fluxos suportados (conforme Documento de Arquitetura Conversacional):
  - RF01: Consultar CMV por período
  - RF02: Consultar estoque
  - RF03: Verificar estoque baixo
  - RF04: Consultar compras e custos
  - RF05: Consultar vendas e perdas
  - RF06: Filtro por período
  - RF07: Solicitar esclarecimentos quando ambíguo
  - RF08: Informar quando dados insuficientes
"""
from __future__ import annotations

import os
import re
import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .core import Ctx, contexto, get_conn, resp

log = logging.getLogger("stockpulse.chatbot")
router = APIRouter(prefix="/chatbot", tags=["chatbot"])

# ---------------------------------------------------------------------------
# Configuração do modelo de linguagem (Gemini via Google AI Studio)
# ---------------------------------------------------------------------------
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

SYSTEM_PROMPT = """Você é o StockPulse AI, assistente de gestão integrado ao StockPulse.
Ajuda gerentes e administradores de restaurantes e lanchonetes a entender seus dados de CMV,
estoque, compras, vendas e perdas.

REGRAS OBRIGATÓRIAS:
1. Use SEMPRE os dados fornecidos pelo sistema — nunca invente valores.
2. Responda em português brasileiro, de forma objetiva e amigável.
3. Se os dados indicarem que não há registros suficientes, informe isso claramente.
4. Quando o usuário perguntar algo vago, identifique a intenção e extraia entidades.
5. Nunca calcule CMV por conta própria — use sempre o valor calculado pela API.
6. Seja conciso: evite respostas longas demais para uma tela de celular.
7. Se precisar de mais informações (ex: período), pergunte de forma direta.

FORMATO DA RESPOSTA (JSON puro, sem markdown):
{
  "intent": "<intent identificada>",
  "needs_clarification": false,
  "clarification_question": null,
  "data_query": {
    "type": "<cmv|estoque|estoque_baixo|compras|vendas|perdas|fechamentos|produto>",
    "produto": "<nome ou null>",
    "periodo": "<hoje|ontem|semana|mes|mes_passado|custom>",
    "data_inicio": "<YYYY-MM-DD ou null>",
    "data_fim": "<YYYY-MM-DD ou null>"
  },
  "response_text": "<resposta final para o usuário>"
}

Intents possíveis: consultar_cmv, consultar_estoque, verificar_estoque_baixo,
consultar_compras, analisar_custos, consultar_vendas, consultar_perdas,
saudacao, ajuda, desconhecido.
"""

# ---------------------------------------------------------------------------
# Modelos de dados da API
# ---------------------------------------------------------------------------

class MensagemIn(BaseModel):
    mensagem: str = Field(min_length=1, max_length=1000)
    historico: list[dict] = Field(default_factory=list, max_length=20)


class RespostaChatbot(BaseModel):
    resposta: str
    intent: str
    dados: Optional[dict] = None
    sugestoes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Utilitários de período
# ---------------------------------------------------------------------------

def resolver_periodo(periodo: str, data_inicio: str | None, data_fim: str | None) -> tuple[date, date]:
    hoje = date.today()
    match periodo:
        case "hoje":
            return hoje, hoje
        case "ontem":
            d = hoje - timedelta(days=1)
            return d, d
        case "semana":
            inicio = hoje - timedelta(days=hoje.weekday())
            return inicio, hoje
        case "mes":
            return hoje.replace(day=1), hoje
        case "mes_passado":
            fim = hoje.replace(day=1) - timedelta(days=1)
            return fim.replace(day=1), fim
        case "custom" if data_inicio and data_fim:
            return date.fromisoformat(data_inicio), date.fromisoformat(data_fim)
        case _:
            # padrão: mês atual
            return hoje.replace(day=1), hoje


# ---------------------------------------------------------------------------
# Consultas ao banco de dados
# ---------------------------------------------------------------------------

def _consultar_cmv(conn, ctx: Ctx, inicio: date, fim: date) -> dict:
    """Busca fechamentos do período e calcula indicadores de CMV."""
    rows = conn.execute(
        """SELECT data, faturamento, cmv_pct, acima_da_meta, perdas
           FROM fechamentos_diarios
           WHERE estabelecimento_id = %s
             AND status = 'fechado'
             AND data BETWEEN %s AND %s
           ORDER BY data""",
        (ctx.estab, inicio, fim)
    ).fetchall()

    if not rows:
        return {"encontrado": False, "inicio": inicio.isoformat(), "fim": fim.isoformat()}

    faturamento_total = sum(r["faturamento"] for r in rows)
    cmv_medio = sum(r["cmv_pct"] for r in rows) / len(rows)
    acima_meta = sum(1 for r in rows if r["acima_da_meta"])
    perdas_total = sum(r["perdas"] for r in rows)
    meta = conn.execute(
        "SELECT meta_cmv_pct FROM estabelecimentos WHERE id = %s", (ctx.estab,)
    ).fetchone()["meta_cmv_pct"]

    return {
        "encontrado": True,
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "dias": len(rows),
        "faturamento_total": float(faturamento_total),
        "cmv_medio_pct": float(cmv_medio.quantize(Decimal("0.1"))),
        "meta_cmv_pct": float(meta),
        "dias_acima_meta": acima_meta,
        "perdas_total": float(perdas_total),
    }


def _consultar_estoque(conn, ctx: Ctx, produto: str | None = None) -> dict:
    """Lista estoque atual, opcionalmente filtrado por produto."""
    if produto:
        rows = conn.execute(
            """SELECT p.nome produto, p.unidade, p.custo_medio custo,
                      COALESCE(SUM(m.quantidade), 0) saldo
               FROM produtos p
               LEFT JOIN movimentacoes_estoque m ON m.produto_id = p.id
               WHERE p.estabelecimento_id = %s AND p.ativo
                 AND LOWER(p.nome) LIKE LOWER(%s)
               GROUP BY p.id ORDER BY p.nome""",
            (ctx.estab, f"%{produto}%")
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT p.nome produto, p.unidade, p.custo_medio custo,
                      COALESCE(SUM(m.quantidade), 0) saldo
               FROM produtos p
               LEFT JOIN movimentacoes_estoque m ON m.produto_id = p.id
               WHERE p.estabelecimento_id = %s AND p.ativo
               GROUP BY p.id ORDER BY p.nome""",
            (ctx.estab,)
        ).fetchall()

    itens = [dict(r) for r in rows]
    for i in itens:
        i["saldo"] = float(i["saldo"])
        i["custo"] = float(i["custo"])
        i["valor_total"] = round(max(i["saldo"], 0) * i["custo"], 2)

    return {"encontrado": bool(itens), "itens": itens, "total_itens": len(itens)}


def _verificar_estoque_baixo(conn, ctx: Ctx, limite_pct: float = 0.2) -> dict:
    """
    Identifica produtos com saldo zero ou muito baixo.
    Como o StockPulse não tem campo 'estoque_minimo', usamos saldo <= 0 ou
    saldo < 20% do total histórico de entradas como heurística.
    """
    rows = conn.execute(
        """SELECT p.nome produto, p.unidade,
                  COALESCE(SUM(m.quantidade), 0) saldo,
                  COALESCE(SUM(CASE WHEN m.quantidade > 0 THEN m.quantidade ELSE 0 END), 0) entradas
           FROM produtos p
           LEFT JOIN movimentacoes_estoque m ON m.produto_id = p.id
           WHERE p.estabelecimento_id = %s AND p.ativo
           GROUP BY p.id
           HAVING COALESCE(SUM(m.quantidade), 0) <= 0
              OR (COALESCE(SUM(CASE WHEN m.quantidade > 0 THEN m.quantidade ELSE 0 END), 0) > 0
                  AND COALESCE(SUM(m.quantidade), 0) <
                      COALESCE(SUM(CASE WHEN m.quantidade > 0 THEN m.quantidade ELSE 0 END), 0) * %s)
           ORDER BY saldo""",
        (ctx.estab, limite_pct)
    ).fetchall()

    itens = [{"produto": r["produto"], "unidade": r["unidade"], "saldo": float(r["saldo"])} for r in rows]
    return {"encontrado": bool(itens), "itens": itens, "total": len(itens)}


def _consultar_compras(conn, ctx: Ctx, inicio: date, fim: date, produto: str | None = None) -> dict:
    """Lista compras do período."""
    base_sql = """
        SELECT c.data, f.nome fornecedor, p.nome produto,
               i.quantidade, i.custo_unit,
               ROUND(i.quantidade * i.custo_unit, 2) total
        FROM compras c
        JOIN itens_compra i ON i.compra_id = c.id
        JOIN produtos p ON p.id = i.produto_id
        LEFT JOIN fornecedores f ON f.id = c.fornecedor_id
        WHERE c.estabelecimento_id = %s AND c.data BETWEEN %s AND %s
    """
    params: list = [ctx.estab, inicio, fim]
    if produto:
        base_sql += " AND LOWER(p.nome) LIKE LOWER(%s)"
        params.append(f"%{produto}%")
    base_sql += " ORDER BY c.data DESC, c.criado_em DESC LIMIT 50"

    rows = conn.execute(base_sql, params).fetchall()
    itens = [dict(r) for r in rows]
    for i in itens:
        i["quantidade"] = float(i["quantidade"])
        i["custo_unit"] = float(i["custo_unit"])
        i["total"] = float(i["total"])
        i["data"] = i["data"].isoformat() if hasattr(i["data"], "isoformat") else str(i["data"])

    total_gasto = sum(i["total"] for i in itens)
    return {
        "encontrado": bool(itens),
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "compras": itens,
        "total_gasto": round(total_gasto, 2),
        "num_compras": len(itens),
    }


def _consultar_vendas(conn, ctx: Ctx, inicio: date, fim: date) -> dict:
    """Resumo de vendas do período."""
    rows = conn.execute(
        """SELECT f.nome ficha, SUM(v.quantidade) quantidade,
                  ROUND(SUM(v.quantidade * v.preco_unit), 2) faturamento,
                  SUM(v.custo_teorico) custo
           FROM vendas v
           JOIN fichas_tecnicas f ON f.id = v.ficha_id
           WHERE v.estabelecimento_id = %s AND v.data BETWEEN %s AND %s
           GROUP BY f.nome ORDER BY faturamento DESC LIMIT 20""",
        (ctx.estab, inicio, fim)
    ).fetchall()

    if not rows:
        return {"encontrado": False, "inicio": inicio.isoformat(), "fim": fim.isoformat()}

    itens = [{"ficha": r["ficha"], "quantidade": float(r["quantidade"]),
              "faturamento": float(r["faturamento"]), "custo": float(r["custo"])} for r in rows]
    total_fat = sum(i["faturamento"] for i in itens)
    return {
        "encontrado": True,
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "produtos_mais_vendidos": itens[:5],
        "total_faturamento": round(total_fat, 2),
        "total_itens_distintos": len(itens),
    }


def _consultar_perdas(conn, ctx: Ctx, inicio: date, fim: date) -> dict:
    """Resumo de perdas do período."""
    rows = conn.execute(
        """SELECT p.nome produto, x.motivo, SUM(x.quantidade) quantidade, SUM(x.valor) valor
           FROM perdas x
           JOIN produtos p ON p.id = x.produto_id
           WHERE x.estabelecimento_id = %s AND x.data BETWEEN %s AND %s
           GROUP BY p.nome, x.motivo ORDER BY valor DESC""",
        (ctx.estab, inicio, fim)
    ).fetchall()

    itens = [{"produto": r["produto"], "motivo": r["motivo"],
              "quantidade": float(r["quantidade"]), "valor": float(r["valor"])} for r in rows]
    total_valor = sum(i["valor"] for i in itens)
    return {
        "encontrado": bool(itens),
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "perdas": itens,
        "total_valor": round(total_valor, 2),
    }


def _analisar_custos(conn, ctx: Ctx, produto: str | None, inicio: date, fim: date) -> dict:
    """Analisa evolução de custo de um produto via compras."""
    if not produto:
        # Sem produto: lista top insumos por gasto
        rows = conn.execute(
            """SELECT p.nome produto, SUM(i.quantidade * i.custo_unit) total
               FROM compras c JOIN itens_compra i ON i.compra_id = c.id
               JOIN produtos p ON p.id = i.produto_id
               WHERE c.estabelecimento_id = %s AND c.data BETWEEN %s AND %s
               GROUP BY p.nome ORDER BY total DESC LIMIT 5""",
            (ctx.estab, inicio, fim)
        ).fetchall()
        return {
            "tipo": "top_custos",
            "encontrado": bool(rows),
            "inicio": inicio.isoformat(),
            "fim": fim.isoformat(),
            "top_insumos": [{"produto": r["produto"], "total": float(r["total"])} for r in rows]
        }

    # Com produto: histórico de preço
    rows = conn.execute(
        """SELECT c.data, i.custo_unit, i.quantidade
           FROM compras c JOIN itens_compra i ON i.compra_id = c.id
           JOIN produtos p ON p.id = i.produto_id
           WHERE c.estabelecimento_id = %s AND LOWER(p.nome) LIKE LOWER(%s)
             AND c.data BETWEEN %s AND %s
           ORDER BY c.data""",
        (ctx.estab, f"%{produto}%", inicio, fim)
    ).fetchall()

    if not rows:
        return {"tipo": "historico_produto", "encontrado": False, "produto": produto}

    historico = [{"data": r["data"].isoformat(), "custo_unit": float(r["custo_unit"]),
                  "quantidade": float(r["quantidade"])} for r in rows]
    variacao = None
    if len(historico) >= 2:
        primeiro = historico[0]["custo_unit"]
        ultimo = historico[-1]["custo_unit"]
        if primeiro > 0:
            variacao = round((ultimo - primeiro) / primeiro * 100, 1)

    return {
        "tipo": "historico_produto",
        "encontrado": True,
        "produto": produto,
        "historico": historico,
        "custo_atual": historico[-1]["custo_unit"] if historico else None,
        "variacao_pct": variacao,
    }


# ---------------------------------------------------------------------------
# Interpretação via Gemini
# ---------------------------------------------------------------------------

def _build_context_msg(historico: list[dict]) -> str:
    if not historico:
        return ""
    lines = ["\n\nHistórico da conversa:"]
    for h in historico[-6:]:
        role = "Usuário" if h.get("role") == "user" else "Assistente"
        lines.append(f"{role}: {h.get('content', '')}")
    return "\n".join(lines)


def _interpretar_com_gemini(mensagem: str, historico: list[dict]) -> dict:
    """Chama o Gemini para identificar intenção, entidades e gerar resposta draft."""
    if not GEMINI_KEY:
        # Fallback sem LLM: usa heurística simples
        return _interpretar_heuristico(mensagem)

    hoje = date.today().isoformat()
    prompt = (
        f"Data de hoje: {hoje}\n"
        f"Mensagem do usuário: {mensagem}"
        + _build_context_msg(historico)
        + "\n\nRetorne APENAS o JSON conforme o formato pedido, sem ```json ou markdown."
    )

    try:
        r = httpx.post(
            f"{GEMINI_URL}?key={GEMINI_KEY}",
            json={
                "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 800},
            },
            timeout=15,
        )
        r.raise_for_status()
        raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        # Limpa possível markdown residual
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`")
        return json.loads(raw)
    except Exception as e:
        log.warning("Gemini indisponível (%s), usando heurística", e)
        return _interpretar_heuristico(mensagem)


def _interpretar_heuristico(msg: str) -> dict:
    """Interpretação offline baseada em palavras-chave (RF07, RF08 fallback)."""
    m = msg.lower()
    hoje = date.today()

    # Período
    if "hoje" in m:
        periodo, di, df = "hoje", hoje.isoformat(), hoje.isoformat()
    elif "ontem" in m:
        d = hoje - timedelta(days=1)
        periodo, di, df = "ontem", d.isoformat(), d.isoformat()
    elif "semana" in m:
        ini = hoje - timedelta(days=hoje.weekday())
        periodo, di, df = "semana", ini.isoformat(), hoje.isoformat()
    elif "mês passado" in m or "mes passado" in m:
        fim = hoje.replace(day=1) - timedelta(days=1)
        periodo, di, df = "mes_passado", fim.replace(day=1).isoformat(), fim.isoformat()
    else:
        periodo, di, df = "mes", hoje.replace(day=1).isoformat(), hoje.isoformat()

    # Intent
    if any(w in m for w in ["cmv", "custo de mercadoria", "fechamento"]):
        intent = "consultar_cmv"
        dtype = "cmv"
    elif any(w in m for w in ["estoque baixo", "perto de acabar", "acabando", "repor"]):
        intent = "verificar_estoque_baixo"
        dtype = "estoque_baixo"
    elif any(w in m for w in ["estoque", "saldo", "quanto tem", "quantas unidade"]):
        intent = "consultar_estoque"
        dtype = "estoque"
    elif any(w in m for w in ["compra", "gastei", "fornecedor"]):
        intent = "consultar_compras"
        dtype = "compras"
    elif any(w in m for w in ["venda", "vendi", "faturei", "faturamento"]):
        intent = "consultar_vendas"
        dtype = "vendas"
    elif any(w in m for w in ["perda", "descarte", "vencimento", "quebra"]):
        intent = "consultar_perdas"
        dtype = "perdas"
    elif any(w in m for w in ["custo", "aumentou", "preço", "caro"]):
        intent = "analisar_custos"
        dtype = "produto"
    elif any(w in m for w in ["olá", "oi", "bom dia", "boa tarde", "boa noite", "hello"]):
        intent = "saudacao"
        dtype = "cmv"
    else:
        intent = "ajuda"
        dtype = "cmv"

    return {
        "intent": intent,
        "needs_clarification": False,
        "clarification_question": None,
        "data_query": {"type": dtype, "produto": None, "periodo": periodo, "data_inicio": di, "data_fim": df},
        "response_text": "",
    }


# ---------------------------------------------------------------------------
# Geração de resposta com dados reais
# ---------------------------------------------------------------------------

def _gerar_resposta_com_dados(intent: str, dados: dict, resposta_llm: str) -> tuple[str, list[str]]:
    """Monta a resposta final combinando template + dados reais do banco."""
    sugestoes: list[str] = []

    if intent == "saudacao":
        return (
            "👋 Olá! Sou o **StockPulse AI**, seu assistente de gestão.\n\n"
            "Posso te ajudar com:\n"
            "• 📊 CMV do período\n• 📦 Estoque e reposição\n"
            "• 🛒 Compras e custos\n• 💰 Vendas e perdas\n\n"
            "O que você gostaria de saber?",
            ["Qual meu CMV este mês?", "Quais produtos estão com estoque baixo?", "Quanto gastei em compras?"]
        )

    if intent == "ajuda":
        return (
            "Posso responder perguntas como:\n"
            "• \"Qual foi meu CMV esta semana?\"\n"
            "• \"Quais produtos estão com estoque baixo?\"\n"
            "• \"Quanto vendi ontem?\"\n"
            "• \"Quais foram minhas perdas este mês?\"\n"
            "• \"Por que o custo do hambúrguer aumentou?\"\n\n"
            "Me conte o que você quer saber! 😊",
            []
        )

    if intent == "consultar_cmv":
        if not dados.get("encontrado"):
            return (
                f"Não encontrei fechamentos registrados entre **{dados['inicio']}** e **{dados['fim']}**.\n\n"
                "Verifique se os fechamentos diários foram realizados no período.",
                ["Ver CMV do mês passado", "Como fazer um fechamento?"]
            )
        cmv = dados["cmv_medio_pct"]
        meta = dados["meta_cmv_pct"]
        status = "✅ Abaixo da meta" if cmv <= meta else "⚠️ Acima da meta"
        texto = (
            f"📊 **CMV de {dados['inicio']} a {dados['fim']}**\n\n"
            f"• CMV médio: **{cmv}%** (meta: {meta}%) — {status}\n"
            f"• Faturamento total: R$ {dados['faturamento_total']:,.2f}\n"
            f"• Dias analisados: {dados['dias']}\n"
            f"• Dias acima da meta: {dados['dias_acima_meta']}\n"
            f"• Perdas totais: R$ {dados['perdas_total']:,.2f}"
        )
        sugestoes = ["Comparar com mês passado", "Ver vendas do período", "Ver perdas do período"]

    elif intent == "consultar_estoque":
        if not dados.get("encontrado"):
            return "Não encontrei produtos cadastrados no estoque.", ["Cadastrar produto"]
        itens = dados["itens"]
        if len(itens) == 1:
            i = itens[0]
            texto = (
                f"📦 **{i['produto']}**\n\n"
                f"• Saldo: **{i['saldo']} {i['unidade']}**\n"
                f"• Custo médio: R$ {i['custo']:.2f}/{i['unidade']}\n"
                f"• Valor em estoque: R$ {i['valor_total']:.2f}"
            )
        else:
            linhas = [f"📦 **Estoque atual** ({dados['total_itens']} itens)\n"]
            for i in itens[:10]:
                saldo_str = f"{i['saldo']:.2f} {i['unidade']}"
                linhas.append(f"• {i['produto']}: **{saldo_str}** (R$ {i['valor_total']:.2f})")
            if len(itens) > 10:
                linhas.append(f"_...e mais {len(itens)-10} itens_")
            texto = "\n".join(linhas)
        sugestoes = ["Quais itens estão com estoque baixo?", "Ver compras recentes"]

    elif intent == "verificar_estoque_baixo":
        if not dados.get("encontrado"):
            texto = "✅ Ótimo! Nenhum item está com estoque crítico no momento."
            sugestoes = ["Ver estoque completo", "Ver compras do mês"]
        else:
            linhas = [f"⚠️ **{dados['total']} item(s) com estoque baixo ou zerado:**\n"]
            for i in dados["itens"]:
                saldo_str = f"{i['saldo']:.2f} {i['unidade']}" if i["saldo"] > 0 else "**ZERADO**"
                linhas.append(f"• {i['produto']}: {saldo_str}")
            linhas.append("\nRecomendo verificar a necessidade de reposição.")
            texto = "\n".join(linhas)
            sugestoes = ["Ver histórico de compras desses itens", "Registrar compra"]

    elif intent == "consultar_compras":
        if not dados.get("encontrado"):
            return (
                f"Não encontrei compras entre **{dados['inicio']}** e **{dados['fim']}**.",
                ["Ver compras do mês passado"]
            )
        texto = (
            f"🛒 **Compras de {dados['inicio']} a {dados['fim']}**\n\n"
            f"• Total gasto: **R$ {dados['total_gasto']:,.2f}**\n"
            f"• Registros encontrados: {dados['num_compras']}\n"
        )
        if dados["compras"][:3]:
            texto += "\n**Últimas compras:**\n"
            for c in dados["compras"][:3]:
                texto += f"• {c['produto']} — {c['quantidade']} × R$ {c['custo_unit']:.2f} = R$ {c['total']:.2f}\n"
        sugestoes = ["Ver custos por insumo", "Ver CMV do período"]

    elif intent == "analisar_custos":
        if dados.get("tipo") == "top_custos":
            if not dados.get("encontrado"):
                return "Sem compras registradas no período para analisar custos.", []
            linhas = [f"💰 **Top insumos por custo ({dados['inicio']} a {dados['fim']})**\n"]
            for i, item in enumerate(dados.get("top_insumos", []), 1):
                linhas.append(f"{i}. {item['produto']}: R$ {item['total']:,.2f}")
            texto = "\n".join(linhas)
        else:
            if not dados.get("encontrado"):
                return f"Não encontrei compras do produto **{dados.get('produto', '?')}** no período.", []
            var = dados.get("variacao_pct")
            var_str = f"{var:+.1f}%" if var is not None else "sem variação calculada"
            texto = (
                f"📈 **Análise de custo: {dados.get('produto')}**\n\n"
                f"• Custo atual: R$ {dados.get('custo_atual', 0):.2f}\n"
                f"• Variação no período: {var_str}\n"
                f"• Compras registradas: {len(dados.get('historico', []))}"
            )
        sugestoes = ["Ver compras do período", "Ver CMV"]

    elif intent == "consultar_vendas":
        if not dados.get("encontrado"):
            return (
                f"Não encontrei vendas registradas entre **{dados['inicio']}** e **{dados['fim']}**.",
                ["Ver vendas de outra data"]
            )
        texto = (
            f"💰 **Vendas de {dados['inicio']} a {dados['fim']}**\n\n"
            f"• Faturamento total: **R$ {dados['total_faturamento']:,.2f}**\n"
            f"• Produtos distintos vendidos: {dados['total_itens_distintos']}\n\n"
            f"**Mais vendidos:**\n"
        )
        for p in dados.get("produtos_mais_vendidos", [])[:5]:
            texto += f"• {p['ficha']}: {p['quantidade']:.0f}× — R$ {p['faturamento']:.2f}\n"
        sugestoes = ["Ver CMV do período", "Ver perdas do período"]

    elif intent == "consultar_perdas":
        if not dados.get("encontrado"):
            return (
                f"Nenhuma perda registrada entre **{dados['inicio']}** e **{dados['fim']}**. 👍",
                []
            )
        texto = (
            f"🗑️ **Perdas de {dados['inicio']} a {dados['fim']}**\n\n"
            f"• Valor total perdido: **R$ {dados['total_valor']:,.2f}**\n\n"
        )
        for p in dados.get("perdas", [])[:5]:
            texto += f"• {p['produto']} ({p['motivo']}): {p['quantidade']} un. — R$ {p['valor']:.2f}\n"
        sugestoes = ["Ver CMV do período", "Ver estoque atual"]

    else:
        texto = resposta_llm or "Desculpe, não consegui interpretar sua pergunta. Tente perguntar sobre CMV, estoque, compras, vendas ou perdas."

    return texto, sugestoes


# ---------------------------------------------------------------------------
# Rota principal do chatbot
# ---------------------------------------------------------------------------

@router.post("", response_model=None)
def chat(body: MensagemIn, ctx: Ctx = Depends(contexto), conn=Depends(get_conn)):
    """
    Endpoint principal do chatbot.
    Recebe mensagem + histórico, interpreta com IA, consulta dados reais e retorna resposta.
    """
    # 1. Interpretação (LLM ou heurística)
    interpretacao = _interpretar_com_gemini(body.mensagem, body.historico)

    # 2. Se precisa de esclarecimento
    if interpretacao.get("needs_clarification") and interpretacao.get("clarification_question"):
        return resp({
            "resposta": interpretacao["clarification_question"],
            "intent": interpretacao.get("intent", "desconhecido"),
            "dados": None,
            "sugestoes": [],
        })

    # 3. Executar consulta de dados
    dq = interpretacao.get("data_query", {})
    dtype = dq.get("type", "cmv")
    produto = dq.get("produto")
    periodo = dq.get("periodo", "mes")
    inicio, fim = resolver_periodo(periodo, dq.get("data_inicio"), dq.get("data_fim"))

    intent = interpretacao.get("intent", "desconhecido")
    dados: dict = {}

    try:
        match intent:
            case "consultar_cmv":
                dados = _consultar_cmv(conn, ctx, inicio, fim)
            case "consultar_estoque":
                dados = _consultar_estoque(conn, ctx, produto)
            case "verificar_estoque_baixo":
                dados = _verificar_estoque_baixo(conn, ctx)
            case "consultar_compras":
                dados = _consultar_compras(conn, ctx, inicio, fim, produto)
            case "analisar_custos":
                dados = _analisar_custos(conn, ctx, produto, inicio, fim)
            case "consultar_vendas":
                dados = _consultar_vendas(conn, ctx, inicio, fim)
            case "consultar_perdas":
                dados = _consultar_perdas(conn, ctx, inicio, fim)
            case _:
                dados = {}
    except Exception as e:
        log.error("Erro ao consultar dados para intent %s: %s", intent, e)
        return resp({
            "resposta": "⚠️ Ocorreu um erro ao consultar os dados. Tente novamente em instantes.",
            "intent": intent,
            "dados": None,
            "sugestoes": [],
        })

    # 4. Gerar resposta final
    resposta_texto, sugestoes = _gerar_resposta_com_dados(
        intent, dados, interpretacao.get("response_text", "")
    )

    return resp({
        "resposta": resposta_texto,
        "intent": intent,
        "dados": dados,
        "sugestoes": sugestoes,
    })


@router.get("/saude")
def saude_chatbot():
    """Verifica se a chave do Gemini está configurada."""
    return resp({
        "chatbot": "ok",
        "gemini_configurado": bool(GEMINI_KEY),
        "modelo": "gemini-2.0-flash" if GEMINI_KEY else "heuristico",
    })
