# StockPulse

Sistema de gestão para negócios de alimentação (hamburgueria, restaurante, açaiteria, bar, pizzaria e outros) com **CMV diário** no centro: o dono lança vendas, compras, perdas e estoque, e o sistema mostra quanto custou o que foi vendido e se o dia deu lucro.

> **Estado atual:** protótipo funcional em um único arquivo HTML (`stockpulse.html`) + uma API em FastAPI com PostgreSQL/Supabase (`stockpulse-api.zip`). O protótipo guarda os dados no navegador; a API (v0.4) guarda no banco e já cobre o que o protótipo faz, mais o cardápio público. **Falta ligar a interface à API** (veja [Limitações](#limitações-e-o-que-ainda-falta)).

---

## O que o sistema faz

| Área | O que tem |
|---|---|
| **Painel** | CMV do dia contra a meta, sinais do dia, gráfico "Pulso do CMV", faturamento vs CMV (diário e mensal), mais vendidos (Curva ABC), operação de hoje |
| **Pedidos** | Quadro Novo → Em preparo → Pronto → Concluído; canais balcão, delivery, WhatsApp, mesa e cardápio. Ao concluir, vira venda, entra no CMV e baixa o estoque |
| **Alerta de pedidos** | Banner com Aceitar / Ver pedido / Recusar, contador no menu, som repetido, aba piscando e notificação do navegador |
| **Cardápio digital** | Vitrine com a marca do restaurante (logo, cor, nome, slogan, WhatsApp), sem nenhuma menção ao StockPulse; o cliente monta o pedido e envia pelo WhatsApp |
| **Promoções** | Cria a arte da promoção (imagem 1080×1080 com logo, preço "De/Por", validade) e mostra o impacto na margem e no CMV antes de aplicar o preço no cardápio |
| **Clientes** | Histórico por cliente e recuperação: quem está 15+ dias sem pedir recebe uma mensagem pronta para o WhatsApp |
| **Fechamento diário** | Faturamento vem das vendas; informa estoques e compras; apura CMV real, CMV %, margem bruta e divergência contra o custo teórico |
| **Insumos / Estoque / Compras** | Cadastro, entradas, saídas e contagem física; cada compra atualiza o custo médio ponderado |
| **Produtos e fichas técnicas** | Categoria, preço de venda, ingredientes com custo individual, custo e margem por unidade |
| **Vendas diárias** | Produto + quantidade → faturamento, CMV e lucro bruto do dia, com baixa automática de insumos |
| **Perdas** | Vencimento, descarte e quebra, com valor perdido |
| **Relatórios** | Faturamento vs CMV, Curva ABC, margem de contribuição, histórico, exportação em CSV, reabertura de dia com motivo e histórico de alterações |
| **Ajustes** | Meta de CMV, marca do cardápio e **tipo de negócio** (modelos de insumos e produtos para hamburgueria, restaurante, açaiteria, bar, pizzaria ou em branco) |
| **Perfis** | Administrador, gerente e operador (o operador não fecha o dia, não cadastra produtos nem altera preços) |

### Como o CMV é calculado

```
CMV            = estoque inicial + compras líquidas − estoque final
CMV %          = CMV ÷ faturamento × 100
Margem bruta   = faturamento − CMV          (não é lucro líquido)
Custo da ficha = Σ (quantidade do insumo × custo do insumo) + outros custos
Custo médio    = (saldo × custo atual + qtd comprada × custo da compra) ÷ (saldo + qtd comprada)
```

Compras do dia não são automaticamente CMV do dia: o CMV usa a contagem física do estoque. A diferença entre o CMV real e o custo teórico (das fichas) é um **sinal para investigar**, não prova de desperdício.

---

## Estrutura

```
stockpulse.html            Protótipo completo (interface + lógica), arquivo único
schema.sql                 Esquema PostgreSQL (Supabase)
stockpulse-api.zip         API
└── stockpulse-api/
    ├── app/
    │   ├── calc.py        Regras de cálculo (funções puras, em Decimal)
    │   ├── core.py        Conexão, autenticação, permissões
    │   ├── auth.py        Integração com o Supabase Auth
    │   ├── operacao.py    Produtos, estoque, compras, fichas (preço, promoção), vendas, perdas
    │   ├── pedidos.py     Pedidos (quadro da cozinha) e clientes
    │   ├── config.py      Dados do negócio, marca, modelos por tipo de negócio
    │   ├── publico.py     Cardápio público e pedido do cliente (sem login)
    │   ├── modelos.py     Insumos e produtos iniciais de cada tipo de negócio
    │   └── main.py        Fechamento diário, relatórios, login, CORS
    ├── migrations/        002_gestao.sql (pedidos, marca, cadastro automático), 003 (logos, opcional)
    ├── tests/             138 testes (com PostgreSQL local)
    ├── schema.sql
    ├── GUIA-SUPABASE.md   Passo a passo do banco e do login
    └── requirements.txt
```

---

## Como usar o protótipo

Abra o `stockpulse.html` no navegador (dois cliques). Não precisa de instalação nem de internet, exceto para as fontes.

**Login:** a página usa o login real (Supabase Auth, via API). Não existem contas nem senhas de demonstração. Antes de usar, preencha o endereço da API na linha `<meta name="api-url" content="">` do arquivo e hospede-o em um endereço `https` (veja `GUIA-AUTENTICACAO.md`). Abrir o arquivo direto do computador não funciona, porque o navegador bloqueia as chamadas à API.

- Os dados ficam salvos **no navegador** (localStorage). Para voltar ao início: **Ajustes > Restaurar demonstração**.
- Os clientes, pedidos e vendas que já vêm no sistema são de **demonstração**.
- A meta de CMV de cada tipo de negócio é só uma **sugestão inicial**: ajuste conforme a sua margem.
- Para o alerta com som, clique em **Ativar alertas de pedidos** (o navegador exige um clique antes de tocar áudio). O alerta funciona também com pedidos feitos em outra aba do mesmo navegador, como a do cardápio do cliente (`#cardapio`).

---

## Como rodar a API

Requisitos: Python 3.10+ e um projeto no Supabase. Rode `schema.sql` e depois `migrations/002_gestao.sql` no SQL Editor (passo a passo em `GUIA-SUPABASE.md`).

```powershell
cd stockpulse-api
python -m venv .venv
.venv\Scripts\Activate.ps1          # Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt

$env:DATABASE_URL      = "url do Session pooler do Supabase"
$env:SUPABASE_URL      = "https://SEU-PROJETO.supabase.co"
$env:SUPABASE_ANON_KEY = "chave publishable do projeto"
python -m uvicorn app.main:app --reload
```

A documentação interativa fica em `http://127.0.0.1:8000/docs`. Faça login em `POST /auth/entrar`, copie o `access_token` e cole em **Authorize**.

| Variável | Para quê |
|---|---|
| `DATABASE_URL` | Conexão com o PostgreSQL (Supabase) |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` | Validar e emitir o login (use só a chave **publishable**) |
| `STOCKPULSE_ENV=producao` | Desliga `/docs`, `/redoc` e `/openapi.json` |
| `CORS_ORIGINS` | Endereços do seu site autorizados a chamar a API pelo navegador (separados por vírgula) |
| `STOCKPULSE_PROXY=1` | Só se a API estiver atrás de proxy que define `X-Forwarded-For` (usado no limite de pedidos do cardápio) |
| `STOCKPULSE_TZ` | Fuso do negócio (padrão `America/Sao_Paulo`) |
| `STOCKPULSE_AUTH=cabecalhos` | **Somente desenvolvimento e testes**: aceita cabeçalhos no lugar do login. Nunca use em produção |

### Rotas principais

Login: `POST /auth/entrar`, `GET /eu` · Produtos: `/produtos` · Estoque: `/estoque`, `/estoque/movimentacoes` · Compras: `/compras` · Fichas e preços: `/fichas`, `/fichas/{nome}/preco|promocao|disponibilidade` · Vendas: `/vendas` · Perdas: `/perdas` · Pedidos: `/pedidos`, `/pedidos/{n}/avancar|cancelar|visto` · Clientes: `/clientes` · Negócio: `/estabelecimento`, `/estabelecimento/modelo` · Fechamento: `/fechamentos/sugestao`, `/cmv/calcular`, `/fechamentos`, `/fechamentos/{dia}/reabrir` · Gestão: `/auditoria`, `/relatorios/resumo` · **Público (sem login):** `/publico/{slug}/cardapio`, `/publico/{slug}/pedidos`.

### Testes

```powershell
$env:TEST_DATABASE_URL = "postgresql://postgres:SENHA@127.0.0.1:5432/stockpulse_test"
python -m pytest
```

> **Atenção:** os testes **apagam e recriam** o banco em que rodam. Use apenas um PostgreSQL **local** de teste. Eles recusam bancos que não sejam locais. Sem `TEST_DATABASE_URL`, só os testes de cálculo rodam.

---

## Segurança

- O `schema.sql` ativa **RLS em todas as tabelas**, para que a chave pública do Supabase não leia nem grave nada pela Data API.
- A API conecta com o papel do banco (que ignora RLS) e aplica o isolamento por estabelecimento no código. Os testes confirmam que um estabelecimento nunca vê os dados de outro.
- O perfil de cada pessoa vem da tabela `usuarios`, não do que o cliente envia.
- O cadastro automático (gatilho no banco) só usa `raw_app_meta_data`, definido pelo servidor, para colocar alguém em um estabelecimento existente. O que o usuário digita no cadastro nunca decide a que negócio ou perfil ele pertence (há teste para isso).
- O cardápio público nunca devolve custo, margem nem estoque, e **os preços do pedido vêm sempre do banco**, nunca do cliente. Cor e logo da marca são validados (hexadecimal e `https`), porque entram na página pública.
- Em produção, desligue a documentação da API e restrinja o CORS ao seu site.
- **Nunca** use as chaves `secret` ou `service_role` do Supabase na página ou no código versionado. Não coloque senhas nem URLs de conexão no repositório.
- O **login** da página é real, mas os **dados das telas** (produtos, pedidos, vendas etc.) ainda ficam no navegador e são de demonstração: qualquer pessoa com acesso ao computador pode ler e alterá-los. Isso só termina quando as telas forem ligadas ao banco.

---

## Design

A interface segue o `DESIGN.md` e o `theme.css`: fundo preto, texto em verdes-fosforescentes, cor lima reservada ao botão principal e aos alertas, sem sombras em cards e botões (só na barra superior). O cardápio digital do cliente usa a cor da marca do restaurante.

---

## Limitações e o que ainda falta

**Já feito na API:** preço, categoria e promoção nos produtos, pedidos com fluxo e baixa de estoque, clientes e recuperação, marca e tipo de negócio, modelos iniciais, cardápio público com pedido do cliente, cadastro automático de novo cliente, RLS e Realtime nas tabelas de pedidos, documentação e CORS configuráveis para produção.

**Falta fazer:**

- [ ] **Ligar a interface à API.** O protótipo HTML ainda usa dados do navegador. Falta a tela pública do cardápio do cliente consumindo `/publico/{slug}/cardapio`
- [ ] **Alerta de pedido em tempo real na interface:** o banco já publica os pedidos no Realtime, mas a assinatura no front ainda não foi feita nem testada de ponta a ponta
- [ ] **Convite de funcionários** pela própria API (exige a chave de administrador do Supabase guardada só no servidor). Hoje o vínculo é feito por SQL (veja o guia)
- [ ] **Cobrança recorrente** (planos, teste grátis, Pix/cartão), tela de cadastro de novo cliente e recuperação de senha
- [ ] **Hospedagem de produção** com HTTPS, domínio, monitoramento de erros e backups testados (plano Pro do Supabase para cliente pagante)
- [ ] **Migrações com o CLI do Supabase** e ambientes separados de teste e produção
- [ ] **IA de imagens para promoções.** A arte de hoje vem de um modelo pronto. Exige um serviço de geração de imagens, uma chave e controle de custo
- [ ] **Atendimento por WhatsApp** (API oficial, mensagens e áudios, atendimento 24h). Hoje o botão só abre a conversa com a mensagem pronta
- [ ] **Limite de pedidos do cardápio** que valha entre vários processos do servidor (hoje é por processo; use também o do proxy)
- [ ] Entregas e motoboys, mesas e comandas, financeiro, nota fiscal e impressão de pedido
- [ ] Vários estabelecimentos por conta, limite de tentativas de login e expiração de sessão
- [ ] Exportação em Excel e PDF (hoje só CSV)
- [ ] LGPD: política de privacidade, termos de uso e consentimento para mensagens de recuperação de vendas

---

## Licença

Ainda não definida.
