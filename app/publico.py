"""Cardápio público (vitrine estilo Brendi) de cada restaurante: SEM login, só o que o cliente final pode ver.
Nunca devolve custo, margem, estoque nem identificadores internos. Os preços sempre vêm do banco."""
import html
import json
import time
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from .core import get_conn, ip_do_cliente, resp
from .pedidos import ItemPedidoIn, criar_pedido, telefone

router = APIRouter()
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
    e = conn.execute(
        "select id, slug, nome, marca_nome, slogan, whatsapp, cor_marca, logo_url from estabelecimentos where slug = %s and cardapio_publico",
        (slug,),
    ).fetchone()
    if e is None:
        raise HTTPException(404, "cardápio não encontrado")
    return e


def _loja_vitrine(conn, slug: Optional[str]) -> dict:
    if slug:
        return _loja(conn, slug)
    e = conn.execute(
        "select id, slug, nome, marca_nome, slogan, whatsapp, cor_marca, logo_url from estabelecimentos where cardapio_publico order by criado_em limit 1"
    ).fetchone()
    if e is None:
        raise HTTPException(404, "nenhum cardápio público ativo encontrado")
    return e


def itens_cardapio(conn, estab_id, filtrar_estoque: bool = False) -> list[dict]:
    """Lista fichas ativas com preço > 0. Quando filtrar_estoque=True, oculta itens sem insumo suficiente."""
    if filtrar_estoque:
        rows = conn.execute(
            """select f.categoria, f.nome, f.preco_venda, f.preco_promocional
               from fichas_tecnicas f
               where f.estabelecimento_id = %s
                 and f.ativo
                 and f.preco_venda > 0
                 and not exists (
                   select 1
                   from itens_ficha i
                   where i.ficha_id = f.id
                     and coalesce((
                       select sum(m.quantidade)
                       from movimentacoes_estoque m
                       where m.produto_id = i.produto_id
                     ), 0) < i.quantidade
                 )
               order by f.categoria, f.nome""",
            (estab_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """select categoria, nome, preco_venda, preco_promocional
               from fichas_tecnicas
               where estabelecimento_id = %s and ativo and preco_venda > 0
               order by categoria, nome""",
            (estab_id,),
        ).fetchall()
    cats: dict = {}
    for r in rows:
        promo = r["preco_promocional"]
        cats.setdefault(r["categoria"], []).append({
            "nome": r["nome"],
            "preco": promo or r["preco_venda"],
            "preco_original": r["preco_venda"] if promo else None,
        })
    return [{"nome": c, "itens": i} for c, i in cats.items()]


@router.get("/publico/{slug}/cardapio")
def cardapio(slug: str, em_estoque: bool = Query(False), conn=Depends(get_conn)):
    e = _loja(conn, slug)
    return resp({
        "nome": e["marca_nome"] or e["nome"],
        "slogan": e["slogan"],
        "whatsapp": e["whatsapp"],
        "cor": e["cor_marca"],
        "logo_url": e["logo_url"],
        "categorias": itens_cardapio(conn, e["id"], filtrar_estoque=em_estoque),
    })


class PedidoPublicoIn(BaseModel):
    cliente_nome: str = Field(min_length=1, max_length=80)
    cliente_telefone: Optional[str] = None
    canal: Literal["Cardápio", "Delivery", "Balcão", "Mesa"] = "Cardápio"
    observacao: Optional[str] = Field(default=None, max_length=300)
    itens: list[ItemPedidoIn] = Field(min_length=1, max_length=30)

    _tel = field_validator("cliente_telefone")(lambda cls, v: telefone(v))


@router.post("/publico/{slug}/pedidos")
def fazer_pedido(slug: str, p: PedidoPublicoIn, req: Request, conn=Depends(get_conn)):
    e = _loja(conn, slug)
    limitar((ip_do_cliente(req), slug))
    r = criar_pedido(conn, e["id"], p.cliente_nome, p.cliente_telefone, p.canal, "A combinar", p.observacao, p.itens)
    conn.execute(
        "insert into auditoria (estabelecimento_id, usuario_nome, perfil, acao) values (%s, 'cardápio', 'cliente', %s)",
        (e["id"], f"pedido #{r['numero']} recebido pelo cardápio"),
    )
    limitar((ip_do_cliente(req), slug))
    r = criar_pedido(conn, e["id"], p.cliente_nome, p.cliente_telefone, "Cardápio", "A combinar", p.observacao, p.itens)
    conn.execute("insert into auditoria (estabelecimento_id, usuario_nome, perfil, acao) values (%s, 'cardápio', 'cliente', %s)", (e["id"], f"pedido #{r['numero']} recebido pelo cardápio"))
    conn.commit()
    return resp({"numero": r["numero"], "total": r["total"], "mensagem": "Pedido recebido! Em instantes o restaurante confirma."}, 201)


def _render_vitrine_html(e: dict, categorias: list[dict], mesa_qr: Optional[str] = None) -> str:
    nome = html.escape(e["marca_nome"] or e["nome"])
    cor = html.escape(e["cor_marca"] or "#39ff6a")
    slogan = html.escape(e["slogan"] or "Peça online com praticidade")
    mesa_esc = html.escape(mesa_qr) if mesa_qr else ""
    logo_esc = html.escape(e["logo_url"]) if e["logo_url"] else ""
    logo_html = f'<img src="{logo_esc}" alt="{nome}" class="logo-img"/>' if logo_esc else f'<div class="logo-fallback">{nome[:2].upper()}</div>'
    payload = json.dumps({
        "slug": e["slug"],
        "nome": nome,
        "cor": cor,
        "whatsapp": e["whatsapp"] or "",
        "mesa": mesa_qr or "",
        "categorias": [
            {
                "nome": c["nome"],
                "itens": [
                    {
                        "nome": it["nome"],
                        "preco": float(it["preco"]),
                        "preco_original": float(it["preco_original"]) if it["preco_original"] is not None else None,
                    }
                    for it in c["itens"]
                ],
            }
            for c in categorias
        ],
    }, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>{nome} — Cardápio Digital</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--brand:{cor};--bg:#0d100e;--panel:#151a16;--card:#1b221d;--border:#273229;--text:#f2f7f3;--muted:#8fa394;--danger:#ff5a5a}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;padding-bottom:96px}}
.hero{{background:linear-gradient(135deg,rgba(0,0,0,.6),rgba(0,0,0,.85)),var(--brand);padding:24px 16px 20px;border-bottom:1px solid var(--border)}}
.hero-in{{max-width:760px;margin:0 auto;display:flex;align-items:center;gap:14px}}
.logo-img,.logo-fallback{{width:58px;height:58px;border-radius:14px;object-fit:cover;border:2px solid rgba(255,255,255,.25);flex-shrink:0}}
.logo-fallback{{display:flex;align-items:center;justify-content:center;background:var(--brand);color:#0a0d0a;font-weight:800;font-size:20px}}
.brand-name{{font-size:22px;font-weight:800}}
.brand-slogan{{font-size:13px;color:var(--muted);margin-top:2px}}
.mesa-pill{{display:inline-block;margin-top:6px;background:rgba(57,255,106,.16);border:1px solid var(--brand);color:var(--brand);padding:2px 10px;border-radius:20px;font-size:11px;font-weight:700}}
.wrap{{max-width:760px;margin:0 auto;padding:14px 16px}}
.search{{width:100%;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:10px 14px;color:var(--text);font-size:14px;margin-bottom:12px;outline:none}}
.search:focus{{border-color:var(--brand)}}
.cats{{display:flex;gap:8px;overflow-x:auto;padding-bottom:8px;margin-bottom:14px}}
.cat-btn{{background:var(--panel);border:1px solid var(--border);color:var(--muted);padding:6px 14px;border-radius:20px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap}}
.cat-btn.act{{background:var(--brand);color:#0a0d0a;border-color:var(--brand)}}
.cat-title{{font-size:15px;font-weight:800;margin:16px 0 10px;color:var(--brand)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}}
.item{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px;display:flex;flex-direction:column;justify-content:space-between;gap:10px}}
.item-name{{font-size:15px;font-weight:700}}
.promo-tag{{display:inline-block;background:rgba(255,179,71,.18);color:#ffb347;font-size:10px;font-weight:700;padding:2px 7px;border-radius:12px;margin-left:6px}}
.price-row{{display:flex;align-items:baseline;gap:8px}}
.price-old{{font-size:12px;color:var(--muted);text-decoration:line-through}}
.price-now{{font-size:16px;font-weight:800;color:var(--brand)}}
.add-btn{{background:var(--brand);color:#0a0d0a;border:none;border-radius:8px;padding:8px 12px;font-weight:700;cursor:pointer;font-size:13px}}
.cart-bar{{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);width:min(720px,calc(100% - 24px));background:var(--brand);color:#0a0d0a;border-radius:14px;padding:12px 18px;display:flex;align-items:center;justify-content:space-between;font-weight:800;cursor:pointer;box-shadow:0 8px 28px rgba(0,0,0,.55);z-index:50}}
.cart-bar.hidden{{display:none}}
.modal-bg{{position:fixed;inset:0;background:rgba(0,0,0,.72);display:flex;align-items:flex-end;justify-content:center;z-index:100}}
.modal-bg.hidden{{display:none}}
.modal{{background:var(--panel);border:1px solid var(--border);border-radius:16px 16px 0 0;width:100%;max-width:560px;max-height:92vh;overflow-y:auto;padding:20px}}
.modal-head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}}
.close-btn{{background:transparent;border:none;color:var(--muted);font-size:20px;cursor:pointer}}
.c-row{{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border);gap:8px}}
.qty-ctrl{{display:flex;align-items:center;gap:8px}}
.qty-btn{{width:26px;height:26px;border-radius:6px;border:1px solid var(--border);background:var(--card);color:var(--text);cursor:pointer;font-weight:700}}
.mode-tabs{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}}
.mode-btn{{background:var(--card);border:1px solid var(--border);color:var(--muted);padding:8px;border-radius:8px;cursor:pointer;font-size:12px;font-weight:700}}
.mode-btn.act{{border-color:var(--brand);color:var(--brand);background:rgba(57,255,106,.1)}}
.fg{{margin-bottom:10px}}
.fg label{{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}}
.fg input,.fg textarea{{width:100%;background:var(--card);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:9px 12px;font-size:14px}}
.submit-btn{{width:100%;background:var(--brand);color:#0a0d0a;border:none;border-radius:10px;padding:12px;font-size:15px;font-weight:800;cursor:pointer;margin-top:6px}}
.ok-box{{background:rgba(57,255,106,.12);border:1px solid var(--brand);border-radius:10px;padding:14px;text-align:center;margin-top:10px}}
.empty{{color:var(--muted);text-align:center;padding:40px 16px}}
</style>
</head>
<body>
<header class="hero">
  <div class="hero-in">
    {logo_html}
    <div>
      <div class="brand-name">{nome}</div>
      <div class="brand-slogan">{slogan}</div>
      {"<div class='mesa-pill'>QR Code · Mesa " + mesa_esc + "</div>" if mesa_esc else ""}
    </div>
  </div>
</header>

<main class="wrap">
  <input id="q" class="search" placeholder="Buscar no cardápio..." oninput="renderMenu()"/>
  <div id="cats" class="cats"></div>
  <div id="menu"></div>
</main>

<div id="cartBar" class="cart-bar hidden" onclick="openCart()">
  <span id="cartCount">0 itens</span>
  <span>Ver carrinho · <span id="cartTotal">R$ 0,00</span></span>
</div>

<div id="cartModal" class="modal-bg hidden">
  <div class="modal">
    <div class="modal-head">
      <strong>Seu Pedido</strong>
      <button class="close-btn" onclick="closeCart()">✕</button>
    </div>
    <div id="cartItems"></div>
    <div class="mode-tabs">
      <button type="button" class="mode-btn" id="mDelivery" onclick="setMode('Delivery')">🛵 Delivery</button>
      <button type="button" class="mode-btn" id="mRetirada" onclick="setMode('Balcão')">🛍 Retirada</button>
      <button type="button" class="mode-btn" id="mMesa" onclick="setMode('Mesa')">🍽 Mesa</button>
    </div>
    <form id="orderForm" onsubmit="sendOrder(event)">
      <div class="fg"><label>Seu nome *</label><input id="cNome" required placeholder="Nome completo"/></div>
      <div class="fg"><label>WhatsApp / Telefone</label><input id="cTel" placeholder="(11) 99999-9999"/></div>
      <div class="fg" id="fgExtra"><label id="lblExtra">Endereço de entrega</label><input id="cExtra" placeholder="Rua, número, bairro"/></div>
      <div class="fg"><label>Observações</label><textarea id="cObs" rows="2" placeholder="Sem cebola, troco para..."></textarea></div>
      <button type="submit" class="submit-btn" id="btnEnviar">Confirmar pedido</button>
      <div id="orderMsg"></div>
    </form>
  </div>
</div>

<script>
const DATA = {payload};
let activeCat = 'Todas';
let cart = {{}};
let mode = DATA.mesa ? 'Mesa' : 'Delivery';

const fmt = v => 'R$ ' + (+v || 0).toLocaleString('pt-BR', {{minimumFractionDigits:2, maximumFractionDigits:2}});

function init() {{
  if (DATA.mesa) document.getElementById('cExtra').value = 'Mesa ' + DATA.mesa;
  setMode(mode);
  renderCats();
  renderMenu();
}}

function renderCats() {{
  const cats = ['Todas', ...DATA.categorias.map(c => c.nome)];
  document.getElementById('cats').innerHTML = cats.map(c =>
    `<button class="cat-btn ${{c === activeCat ? 'act' : ''}}" onclick="setCat('${{c.replace(/'/g, "\\'")}}')">${{c}}</button>`
  ).join('');
}}

function setCat(c) {{ activeCat = c; renderCats(); renderMenu(); }}

function renderMenu() {{
  const q = (document.getElementById('q').value || '').trim().toLowerCase();
  const list = DATA.categorias.filter(c => activeCat === 'Todas' || c.nome === activeCat);
  let html = '';
  for (const c of list) {{
    const itens = c.itens.filter(i => !q || i.nome.toLowerCase().includes(q));
    if (!itens.length) continue;
    html += `<div class="cat-title">${{c.nome}}</div><div class="grid">`;
    for (const it of itens) {{
      const promo = it.preco_original ? `<span class="promo-tag">PROMO</span>` : '';
      const oldP = it.preco_original ? `<span class="price-old">${{fmt(it.preco_original)}}</span>` : '';
      html += `<div class="item">
        <div><div class="item-name">${{it.nome}}${{promo}}</div></div>
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div class="price-row">${{oldP}}<span class="price-now">${{fmt(it.preco)}}</span></div>
          <button class="add-btn" onclick="addCart('${{it.nome.replace(/'/g, "\\'")}}', ${{it.preco}})">+ Adicionar</button>
        </div>
      </div>`;
    }}
    html += '</div>';
  }}
  document.getElementById('menu').innerHTML = html || '<div class="empty">Nenhum item disponível em estoque no momento.</div>';
}}

function addCart(nome, preco) {{
  if (!cart[nome]) cart[nome] = {{nome, preco, qtd: 0}};
  cart[nome].qtd += 1;
  updateBar();
}}

function chgQty(nome, delta) {{
  if (!cart[nome]) return;
  cart[nome].qtd += delta;
  if (cart[nome].qtd <= 0) delete cart[nome];
  updateBar();
  renderCartItems();
}}

function updateBar() {{
  const vals = Object.values(cart);
  const count = vals.reduce((a, i) => a + i.qtd, 0);
  const total = vals.reduce((a, i) => a + i.qtd * i.preco, 0);
  document.getElementById('cartCount').textContent = count + (count === 1 ? ' item' : ' itens');
  document.getElementById('cartTotal').textContent = fmt(total);
  document.getElementById('cartBar').classList.toggle('hidden', count === 0);
}}

function setMode(m) {{
  mode = m;
  document.getElementById('mDelivery').classList.toggle('act', m === 'Delivery');
  document.getElementById('mRetirada').classList.toggle('act', m === 'Balcão');
  document.getElementById('mMesa').classList.toggle('act', m === 'Mesa');
  const fg = document.getElementById('fgExtra');
  const lbl = document.getElementById('lblExtra');
  const inp = document.getElementById('cExtra');
  if (m === 'Delivery') {{ fg.style.display = 'block'; lbl.textContent = 'Endereço de entrega *'; inp.placeholder = 'Rua, número, bairro'; }}
  else if (m === 'Mesa') {{ fg.style.display = 'block'; lbl.textContent = 'Número da mesa *'; inp.placeholder = 'Ex: Mesa 5'; }}
  else {{ fg.style.display = 'none'; }}
}}

function openCart() {{ renderCartItems(); document.getElementById('cartModal').classList.remove('hidden'); }}
function closeCart() {{ document.getElementById('cartModal').classList.add('hidden'); }}

function renderCartItems() {{
  const vals = Object.values(cart);
  if (!vals.length) {{ document.getElementById('cartItems').innerHTML = '<div class="empty">Carrinho vazio</div>'; return; }}
  const total = vals.reduce((a, i) => a + i.qtd * i.preco, 0);
  document.getElementById('cartItems').innerHTML = vals.map(i => `
    <div class="c-row">
      <div><strong>${{i.nome}}</strong><div style="font-size:12px;color:var(--muted)">${{fmt(i.preco)}} cada</div></div>
      <div class="qty-ctrl">
        <button type="button" class="qty-btn" onclick="chgQty('${{i.nome.replace(/'/g, "\\'")}}', -1)">-</button>
        <span>${{i.qtd}}</span>
        <button type="button" class="qty-btn" onclick="chgQty('${{i.nome.replace(/'/g, "\\'")}}', 1)">+</button>
      </div>
    </div>`).join('') + `<div style="text-align:right;font-weight:800;margin-top:8px;color:var(--brand)">Total: ${{fmt(total)}}</div>`;
}}

async function sendOrder(e) {{
  e.preventDefault();
  const vals = Object.values(cart);
  if (!vals.length) return;
  const extra = document.getElementById('cExtra').value.trim();
  const obsRaw = document.getElementById('cObs').value.trim();
  const prefix = mode === 'Balcão' ? '[Retirada]' : (extra ? `[${{mode}}: ${{extra}}]` : `[${{mode}}]`);
  const observacao = [prefix, obsRaw].filter(Boolean).join(' ').slice(0, 300);
  const btn = document.getElementById('btnEnviar');
  btn.disabled = true; btn.textContent = 'Enviando...';
  try {{
    const r = await fetch(`/publico/${{DATA.slug}}/pedidos`, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        cliente_nome: document.getElementById('cNome').value.trim(),
        cliente_telefone: document.getElementById('cTel').value.trim() || null,
        canal: mode,
        observacao,
        itens: vals.map(i => ({{ficha: i.nome, quantidade: i.qtd}}))
      }})
    }});
    const d = await r.json();
    if (!r.ok) {{ document.getElementById('orderMsg').innerHTML = `<div style="color:var(--danger);margin-top:8px">${{d.detail || 'Erro ao enviar pedido'}}</div>`; return; }}
    cart = {{}}; updateBar(); renderCartItems();
    document.getElementById('orderMsg').innerHTML = `<div class="ok-box"><strong>Pedido #${{d.numero}} recebido!</strong><br/>Total: ${{fmt(d.total)}}<br/><span style="font-size:12px;color:var(--muted)">${{d.mensagem}}</span></div>`;
  }} catch (err) {{
    document.getElementById('orderMsg').innerHTML = '<div style="color:var(--danger);margin-top:8px">Falha de conexão.</div>';
  }} finally {{
    btn.disabled = false; btn.textContent = 'Confirmar pedido';
  }}
}}

init();
</script>
</body>
</html>"""


@router.get("/cardapio", response_class=HTMLResponse)
def vitrine_padrao(slug: Optional[str] = Query(None), mesa: Optional[str] = Query(None), conn=Depends(get_conn)):
    e = _loja_vitrine(conn, slug)
    cats = itens_cardapio(conn, e["id"], filtrar_estoque=True)
    return HTMLResponse(_render_vitrine_html(e, cats, mesa_qr=mesa))


@router.get("/cardapio/{slug}", response_class=HTMLResponse)
def vitrine_slug(slug: str, mesa: Optional[str] = Query(None), conn=Depends(get_conn)):
    e = _loja(conn, slug)
    cats = itens_cardapio(conn, e["id"], filtrar_estoque=True)
    return HTMLResponse(_render_vitrine_html(e, cats, mesa_qr=mesa))
