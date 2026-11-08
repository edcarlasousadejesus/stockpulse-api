# StockPulse API

Regras de CMV diário centralizadas no back-end (o front apenas exibe).

    python -m venv .venv
    .venv\Scripts\Activate.ps1          # Windows PowerShell  (Linux/Mac: source .venv/bin/activate)
    pip install -r requirements.txt
    $env:DATABASE_URL = "<url do Supabase>"   # veja GUIA-SUPABASE.md
    python -m uvicorn app.main:app --reload   # documentação interativa em /docs

Versão 0.4: pedidos, clientes, marca, modelos por tipo de negócio e cardápio público.

Autenticação: token do Supabase Auth (Authorization: Bearer). Faça login em POST /auth/entrar.
O perfil e o estabelecimento vêm da tabela usuarios. Veja GUIA-SUPABASE.md.
(STOCKPULSE_AUTH=cabecalhos liga o modo de testes com cabeçalhos: nunca use em produção.)

## Rotas
- Produtos: POST/GET /produtos (cadastro só gerente/admin)
- Estoque: GET /estoque, POST /estoque/movimentacoes (entrada, saida, contagem)
- Compras: POST/GET /compras (gera entrada e atualiza o custo médio ponderado)
- Fichas técnicas: POST/GET /fichas (criação só gerente/admin)
- Vendas: POST/GET /vendas (baixa automática dos ingredientes; sinaliza estoque negativo)
- Perdas: POST/GET /perdas (baixa o estoque; calcula o valor)
- Fechamento: GET /fechamentos/sugestao?data=, POST /cmv/calcular, POST /fechamentos,
  GET /fechamentos, POST /fechamentos/{dia}/reabrir (exige motivo)
- Fichas (produtos de venda): categoria, preço, promoção e disponibilidade em /fichas, /fichas/{nome}/preco|promocao|disponibilidade
- Pedidos: POST/GET /pedidos, POST /pedidos/{n}/avancar|cancelar|visto (ao entregar vira venda e baixa o estoque)
- Clientes: GET /clientes (com recuperação de quem está há 15+ dias sem pedir)
- Negócio: GET/PUT /estabelecimento (marca, meta de CMV, cardápio público), POST /estabelecimento/modelo
- Público, sem login: GET /publico/{slug}/cardapio, POST /publico/{slug}/pedidos
- Login: POST /auth/entrar, GET /eu
- Gestão: GET /auditoria, GET /relatorios/resumo

Dados: PostgreSQL (Supabase). Rode schema.sql e depois migrations/*.sql. Passo a passo em GUIA-SUPABASE.md.
Produção: veja a seção 8 do guia (STOCKPULSE_ENV, CORS_ORIGINS, STOCKPULSE_PROXY).
Próximo passo: ligar a página à API e hospedar a API.
