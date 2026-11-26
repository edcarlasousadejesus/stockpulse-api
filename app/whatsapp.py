"""Ponte externa WhatsApp (texto + áudio .ogg via Whisper/Groq) e notificações automáticas de status."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from decimal import Decimal
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from .core import get_conn, resp
from .pedidos import ItemPedidoIn, criar_pedido, telefone
from .publico import itens_cardapio

log = logging.getLogger("stockpulse.whatsapp")
router = APIRouter(tags=["whatsapp"])

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
WHATSAPP_API_URL = os.environ.get("WHATSAPP_API_URL", "")
WHATSAPP_AUDIO_API_URL = os.environ.get("WHATSAPP_AUDIO_API_URL", "")
WHATSAPP_API_TOKEN = os.environ.get("WHATSAPP_API_TOKEN", "")
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "stockpulse-verify")
ELEVENLABS_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

# Registro em memória dos últimos avisos disparados (observabilidade e testes)
_NOTIFICACOES: list[dict] = []

MENSAGENS_STATUS = {
    "novo": "✅ Olá {nome}! Recebemos seu pedido #{numero}. Em breve ele entra em preparo!",
    "preparo": "👨‍🍳 Olá {nome}! Seu pedido #{numero} entrou *Em preparo* na cozinha. Previsão: ~{previsao} min.",
    "pronto": "✅ Olá {nome}! Seu pedido #{numero} está *Pronto*!",
    "entregue": "🎉 Olá {nome}! Seu pedido #{numero} foi *Concluído*. Obrigado pela preferência!",
}

STATUS_COM_TTS = {"novo", "preparo", "pronto"}


def gerar_audio_tts(texto: str, voice: Optional[str] = None) -> Optional[tuple[bytes, str]]:
    """Gera áudio TTS (OpenAI ou ElevenLabs). Retorna (bytes, provedor) ou None para fallback em texto."""
    limpo = re.sub(r"[*`_✅👨‍🍳🎉🍽️💡]", "", texto).strip()
    if not limpo:
        return None
    if OPENAI_KEY:
        try:
            r = httpx.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
                json={
                    "model": os.environ.get("OPENAI_TTS_MODEL", "tts-1"),
                    "voice": voice or os.environ.get("OPENAI_TTS_VOICE", "nova"),
                    "input": limpo,
                    "response_format": "opus",
                },
                timeout=20,
            )
            r.raise_for_status()
            if r.content:
                return r.content, "openai"
        except Exception as e:
            log.warning("Falha no TTS OpenAI (fallback em texto): %s", e)

    if ELEVENLABS_KEY:
        try:
            r = httpx.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
                headers={"xi-api-key": ELEVENLABS_KEY, "Content-Type": "application/json"},
                json={"text": limpo, "model_id": "eleven_multilingual_v2"},
                timeout=20,
            )
            r.raise_for_status()
            if r.content:
                return r.content, "elevenlabs"
        except Exception as e:
            log.warning("Falha no TTS ElevenLabs (fallback em texto): %s", e)

    return None


def enviar_whatsapp(numero_destino: str, texto: str) -> bool:
    """Envia mensagem de texto via gateway WhatsApp configurado (WHATSAPP_API_URL). Best-effort."""
    if not numero_destino:
        return False
    _NOTIFICACOES.append({"telefone": numero_destino, "mensagem": texto, "tipo": "texto"})
    if len(_NOTIFICACOES) > 200:
        del _NOTIFICACOES[:-200]
    if not WHATSAPP_API_URL:
        log.info("WhatsApp (simulado para %s): %s", numero_destino, texto)
        return False
    headers = {"Content-Type": "application/json"}
    if WHATSAPP_API_TOKEN:
        headers["Authorization"] = f"Bearer {WHATSAPP_API_TOKEN}"
    try:
        r = httpx.post(WHATSAPP_API_URL, json={"number": numero_destino, "text": texto}, headers=headers, timeout=5)
        return r.is_success
    except Exception as e:
        log.warning("Falha ao enviar WhatsApp para %s: %s", numero_destino, e)
        return False


def enviar_whatsapp_audio(numero_destino: str, audio_bytes: bytes, legenda: str = "") -> bool:
    """Envia áudio TTS pelo gateway WhatsApp (com fallback automático para texto se o gateway não aceitar áudio)."""
    if not numero_destino:
        return False
    _NOTIFICACOES.append({"telefone": numero_destino, "mensagem": legenda, "tipo": "audio", "audio_bytes": len(audio_bytes)})
    if len(_NOTIFICACOES) > 200:
        del _NOTIFICACOES[:-200]
    destino_url = WHATSAPP_AUDIO_API_URL or WHATSAPP_API_URL
    if not destino_url:
        log.info("WhatsApp Áudio TTS (simulado para %s, %d bytes): %s", numero_destino, len(audio_bytes), legenda)
        return False
    headers = {"Content-Type": "application/json"}
    if WHATSAPP_API_TOKEN:
        headers["Authorization"] = f"Bearer {WHATSAPP_API_TOKEN}"
    b64 = base64.b64encode(audio_bytes).decode()
    payload = {"number": numero_destino, "audio_base64": b64, "mediatype": "audio", "caption": legenda}
    try:
        r = httpx.post(destino_url, json=payload, headers=headers, timeout=10)
        if r.is_success:
            return True
    except Exception as e:
        log.warning("Falha ao enviar áudio WhatsApp para %s: %s", numero_destino, e)
    # Fallback em texto caso o gateway de áudio falhe
    if legenda:
        return enviar_whatsapp(numero_destino, legenda)
    return False


def notificar_status_whatsapp(
    numero: int,
    cliente_nome: str,
    cliente_telefone: Optional[str],
    novo_status: str,
    previsao_min: int = 20,
    conn=None,
    estab_id: Optional[str] = None,
    uid: Optional[str] = None,
) -> Optional[str]:
    """Dispara aviso automático (TTS nos status-chave novo/preparo/pronto, com fallback em texto). Respeita bloqueios."""
    tpl = MENSAGENS_STATUS.get(novo_status)
    if not tpl or not cliente_telefone:
        return None
    voz = None
    if conn is not None and estab_id:
        from .complemento import esta_bloqueado, obter_assistente_estab
        if esta_bloqueado(conn, estab_id, cliente_telefone):
            return None
        voz = obter_assistente_estab(conn, estab_id).get("voz_tts")
    msg = tpl.format(nome=cliente_nome or "cliente", numero=numero, previsao=previsao_min)
    enviado_audio = False
    if novo_status in STATUS_COM_TTS:
        tts = gerar_audio_tts(msg, voice=voz) if voz else gerar_audio_tts(msg)
        if tts:
            audio_bytes, provedor = tts
            enviar_whatsapp_audio(cliente_telefone, audio_bytes, msg)
            enviado_audio = True
            if conn is not None and estab_id:
                from .ia import registrar_uso_ia
                registrar_uso_ia(conn, estab_id, uid, "whatsapp_tts", provedor, {"pedido": numero, "status": novo_status})
    if not enviado_audio:
        enviar_whatsapp(cliente_telefone, msg)
    return msg


def transcrever_audio_ogg(audio_bytes: bytes, filename: str = "audio.ogg") -> str:
    """Transcreve áudio .ogg usando Groq Whisper (preferencial) ou OpenAI Whisper."""
    if not audio_bytes:
        raise HTTPException(422, "áudio vazio")
    if GROQ_KEY:
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        key = GROQ_KEY
        model = "whisper-large-v3-turbo"
    elif OPENAI_KEY:
        url = "https://api.openai.com/v1/audio/transcriptions"
        key = OPENAI_KEY
        model = "whisper-1"
    else:
        raise HTTPException(503, "serviço de transcrição não configurado (defina GROQ_API_KEY ou OPENAI_API_KEY)")

    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            data={"model": model, "language": "pt"},
            files={"file": (filename, audio_bytes, "audio/ogg")},
            timeout=25,
        )
        r.raise_for_status()
        return (r.json().get("text") or "").strip()
    except HTTPException:
        raise
    except Exception as e:
        log.error("Erro na transcrição Whisper/Groq: %s", e)
        raise HTTPException(502, "falha ao transcrever áudio")


def _resolver_estabelecimento(conn, slug: Optional[str], estab_id: Optional[str]) -> dict:
    if estab_id:
        e = conn.execute("select id, nome, marca_nome, slug from estabelecimentos where id = %s", (estab_id,)).fetchone()
    elif slug:
        e = conn.execute("select id, nome, marca_nome, slug from estabelecimentos where slug = %s", (slug,)).fetchone()
    else:
        e = conn.execute("select id, nome, marca_nome, slug from estabelecimentos order by cardapio_publico desc, criado_em limit 1").fetchone()
    if e is None:
        raise HTTPException(404, "estabelecimento não encontrado")
    return e


def _extrair_itens_do_texto(texto: str, itens_disponiveis: list[dict]) -> list[ItemPedidoIn]:
    """Extrai itens e quantidades mencionados no texto comparando com o cardápio real em estoque."""
    t = texto.lower()
    encontrados: list[ItemPedidoIn] = []
    # Ordena por nome mais longo primeiro para evitar match parcial antes do completo
    for item in sorted(itens_disponiveis, key=lambda x: -len(x["nome"])):
        nome = item["nome"]
        nome_l = nome.lower()
        if nome_l in t:
            # Tenta capturar quantidade imediatamente antes do nome (ex: "2 hambúrguer", "2x hambúrguer")
            m = re.search(rf"(\d+)\s*(?:x|un|unidades?)?\s*(?:de\s+)?{re.escape(nome_l)}", t)
            qtd = Decimal(m.group(1)) if m else Decimal("1")
            if Decimal("1") <= qtd <= Decimal("99"):
                encontrados.append(ItemPedidoIn(ficha=nome, quantidade=qtd))
    return encontrados


def _interpretar_mensagem_ia(texto: str, itens_disponiveis: list[dict], assistente: Optional[dict] = None) -> dict:
    """Usa Gemini (quando configurado) ou parser determinístico para decidir ação (cardápio vs pedido confirmado)."""
    if GEMINI_KEY:
        lista = "\n".join(f"- {i['nome']} (R$ {i['preco']})" for i in itens_disponiveis)
        a = assistente or {}
        persona = (
            f"Seu nome é {a.get('nome', 'Atendente StockPulse')}. "
            f"Tom: {a.get('formalidade', 'equilibrado')}. "
            f"{a.get('estilo', '')}"
        ).strip()
        prompt = (
            f"Você é atendente de WhatsApp de um restaurante. {persona}\nCardápio em estoque:\n"
            f"{lista}\n\nMensagem do cliente: {texto}\n\n"
            'Retorne APENAS JSON puro: {"acao": "cardapio|pedido|duvida", "confirmado": true|false, '
            '"pagamento": "Pix|Cartão|Dinheiro|A combinar", "itens": [{"ficha": "nome exato", "quantidade": 1}], "resposta": "texto curto"}'
        )
        try:
            r = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}",
                json={"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1}},
                timeout=12,
            )
            r.raise_for_status()
            raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`")
            return json.loads(raw)
        except Exception as e:
            log.warning("Gemini fallback no WhatsApp (%s)", e)

    t = texto.lower()
    itens = _extrair_itens_do_texto(texto, itens_disponiveis)
    confirmado = bool(itens) and any(
        w in t for w in ("confirm", "fechar", "quero", "manda", "pode pedir", "pedir", "enviar", "fazer pedido")
    )
    pagamento = "Pix" if "pix" in t else "Cartão" if ("cartão" in t or "cartao" in t) else "Dinheiro" if "dinheiro" in t else "A combinar"
    if confirmado:
        return {"acao": "pedido", "confirmado": True, "pagamento": pagamento, "itens": [{"ficha": i.ficha, "quantidade": str(i.quantidade)} for i in itens], "resposta": ""}
    if any(w in t for w in ("cardápio", "cardapio", "menu", "preço", "preco", "tem o que", "disponível", "disponivel")):
        return {"acao": "cardapio", "confirmado": False, "pagamento": pagamento, "itens": [], "resposta": ""}
    return {"acao": "duvida", "confirmado": False, "pagamento": pagamento, "itens": [{"ficha": i.ficha, "quantidade": str(i.quantidade)} for i in itens], "resposta": ""}


@router.get("/webhook/whatsapp")
def verificar_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
):
    """Verificação padrão de webhook (Meta Cloud API)."""
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN and hub_challenge:
        return PlainTextResponse(hub_challenge)
    raise HTTPException(403, "token de verificação inválido")


@router.post("/webhook/whatsapp")
async def webhook_whatsapp(req: Request, conn=Depends(get_conn)):
    """Recebe texto ou áudio (.ogg), transcreve via Whisper/Groq, consulta cardápio/estoque e registra pedidos confirmados."""
    ctype = (req.headers.get("content-type") or "").lower()
    texto = ""
    nome = "Cliente WhatsApp"
    tel_raw = None
    slug = None
    estab_id = None
    audio_bytes: Optional[bytes] = None

    if "multipart/form-data" in ctype:
        try:
            form = await req.form()
        except AssertionError:
            raise HTTPException(415, "instale python-multipart para upload direto ou envie audio_base64/audio_url em JSON")
        texto = str(form.get("texto") or form.get("text") or form.get("body") or "")
        nome = str(form.get("nome") or form.get("pushName") or nome)
        tel_raw = form.get("telefone") or form.get("from") or form.get("sender")
        slug = form.get("slug") or form.get("estabelecimento_slug")
        estab_id = form.get("estabelecimento_id")
        up = form.get("audio") or form.get("file")
        if up is not None and hasattr(up, "read"):
            audio_bytes = await up.read()
    else:
        body = await req.json()
        texto = str(body.get("texto") or body.get("text") or body.get("body") or body.get("message") or "").strip()
        nome = str(body.get("nome") or body.get("pushName") or nome).strip() or "Cliente WhatsApp"
        tel_raw = body.get("telefone") or body.get("from") or body.get("sender")
        slug = body.get("slug") or body.get("estabelecimento_slug")
        estab_id = body.get("estabelecimento_id")
        if body.get("audio_base64"):
            audio_bytes = base64.b64decode(body["audio_base64"])
        elif body.get("audio_url") or body.get("mediaUrl"):
            media_url = body.get("audio_url") or body.get("mediaUrl")
            try:
                ra = httpx.get(media_url, timeout=15)
                ra.raise_for_status()
                audio_bytes = ra.content
            except Exception as e:
                log.warning("Falha ao baixar áudio %s: %s", media_url, e)
                raise HTTPException(502, "não foi possível baixar o áudio")

    tel_limpo = None
    if tel_raw:
        try:
            tel_limpo = telefone(str(tel_raw))
        except ValueError:
            tel_limpo = re.sub(r"\D", "", str(tel_raw)) or None

    transcricao = None
    if audio_bytes is not None:
        transcricao = transcrever_audio_ogg(audio_bytes)
        texto = f"{texto} {transcricao}".strip() if texto else transcricao

    if not texto:
        raise HTTPException(422, "envie texto ou áudio (.ogg)")

    estab = _resolver_estabelecimento(conn, slug, estab_id)
    from .complemento import esta_bloqueado, marcar_carrinho_recuperado, obter_assistente_estab, registrar_carrinho_aberto, sugerir_cross_sell
    if tel_limpo and esta_bloqueado(conn, estab["id"], tel_limpo):
        return resp({"bloqueado": True, "transcricao": transcricao, "resposta": None, "pedido": None})

    assistente = obter_assistente_estab(conn, estab["id"])
    cats = itens_cardapio(conn, estab["id"], filtrar_estoque=True)
    itens_disponiveis = [it for c in cats for it in c["itens"]]

    if not itens_disponiveis:
        msg = "Desculpe, no momento não temos itens disponíveis em estoque no cardápio."
        if tel_limpo:
            enviar_whatsapp(tel_limpo, msg)
        return resp({"resposta": msg, "transcricao": transcricao, "pedido": None})

    interp = _interpretar_mensagem_ia(texto, itens_disponiveis, assistente)
    pedido_criado = None

    if interp.get("confirmado") and interp.get("itens"):
        itens_in = [ItemPedidoIn(ficha=i["ficha"], quantidade=Decimal(str(i["quantidade"]))) for i in interp["itens"]]
        pag = interp.get("pagamento") if interp.get("pagamento") in ("Pix", "Cartão", "Dinheiro", "A combinar") else "A combinar"
        pedido_criado = criar_pedido(
            conn,
            estab["id"],
            nome,
            tel_limpo,
            "WhatsApp",
            pag,
            f"Via WhatsApp: {texto[:240]}",
            itens_in,
        )
        marcar_carrinho_recuperado(conn, estab["id"], tel_limpo)
        conn.execute(
            "insert into auditoria (estabelecimento_id, usuario_nome, perfil, acao) values (%s, 'whatsapp-ia', 'cliente', %s)",
            (estab["id"], f"pedido #{pedido_criado['numero']} confirmado via WhatsApp"),
        )
        conn.commit()
        sug = sugerir_cross_sell(conn, estab["id"], [i.ficha for i in itens_in], limite=2)
        extra_cs = ("\n💡 Que tal adicionar: " + ", ".join(f"{s['nome']} (R$ {s['preco']})" for s in sug) + "?") if sug else ""
        resposta = (
            f"✅ Pedido #{pedido_criado['numero']} confirmado!\n"
            f"Total: R$ {pedido_criado['total']}\n"
            f"Status: Novo · Avisaremos por aqui quando entrar em preparo e ficar pronto.{extra_cs}"
        )
    elif interp.get("acao") == "cardapio":
        linhas = [f"🍽️ *Cardápio disponível ({estab['marca_nome'] or estab['nome']}):*"]
        for it in itens_disponiveis[:15]:
            linhas.append(f"• {it['nome']} — R$ {it['preco']}")
        linhas.append("\nPara pedir, envie os itens e diga *confirmar*!")
        resposta = "\n".join(linhas)
    else:
        if interp.get("itens") and tel_limpo:
            registrar_carrinho_aberto(conn, estab["id"], tel_limpo, nome, interp["itens"], Decimal("0"), "WhatsApp")
            conn.commit()
        sug = sugerir_cross_sell(conn, estab["id"], [i["ficha"] for i in (interp.get("itens") or [])], limite=2)
        extra_cs = ("\n💡 Sugestão para acompanhar: " + ", ".join(f"{s['nome']} (R$ {s['preco']})" for s in sug)) if sug else ""
        resposta = (interp.get("resposta") or (
            f"Olá {nome}! Aqui é {assistente['nome']}. Temos {len(itens_disponiveis)} item(ns) disponível(is) hoje. "
            "Peça o *cardápio* ou informe o que deseja + *confirmar* para registrar seu pedido."
        )) + extra_cs

    if tel_limpo:
        if pedido_criado:
            notificar_status_whatsapp(
                pedido_criado["numero"],
                nome,
                tel_limpo,
                "novo",
                conn=conn,
                estab_id=estab["id"],
            )
            conn.commit()
        else:
            enviar_whatsapp(tel_limpo, resposta)

    return resp({"resposta": resposta, "transcricao": transcricao, "pedido": pedido_criado}, 201 if pedido_criado else 200)
