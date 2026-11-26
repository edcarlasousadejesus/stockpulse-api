# Ligando a API ao Supabase

Os nomes de menus do Supabase podem mudar de vez em quando; se algo não estiver onde está escrito, procure pelo nome do botão.

## 1. Criar o projeto
1. Entre em https://supabase.com, crie uma conta e clique em **New project**.
2. Escolha nome, região (de preferência **South America (São Paulo)**) e uma **senha do banco**. Guarde a senha: ela só é mostrada na criação.

## 2. Criar as tabelas
1. No menu lateral, abra **SQL Editor** e crie uma nova consulta.
2. Cole todo o conteúdo do arquivo `schema.sql` e clique em **Run**. Deve terminar com "Success".
   O script também ativa a segurança RLS em todas as tabelas, para que a chave pública do Supabase não consiga ler os dados.

## 3. Criar o seu estabelecimento
No mesmo SQL Editor, rode:

    insert into estabelecimentos (nome, meta_cmv_pct) values ('Minha Hamburgueria', 28) returning id;

Copie o `id` (um UUID). Ele vai no cabeçalho `X-Estabelecimento` de todas as chamadas.

## 4. Pegar a URL de conexão
1. Clique em **Connect** (botão no topo do projeto) e escolha **Session pooler**.
2. Copie a URL e troque `[YOUR-PASSWORD]` pela senha do banco.
   Se a senha tiver caracteres especiais (@, #, / etc.), codifique-os (@ vira %40, # vira %23, / vira %2F).
   Prefira uma senha só com letras e números.

## 5. Rodar a API
No PowerShell, na pasta do projeto, com o ambiente `(.venv)` ativado:

    pip install -r requirements.txt
    $env:DATABASE_URL = "postgres://postgres.xxxx:SUASENHA@aws-0-sa-east-1.pooler.supabase.com:5432/postgres"
    python -m uvicorn app.main:app --reload

Abra http://127.0.0.1:8000/docs e use o UUID do passo 3 em `x-estabelecimento`
(`x-perfil`: gerente, `x-usuario`: seu nome). Os dados agora ficam gravados no Supabase:
você pode vê-los em **Table Editor**, e eles continuam depois de reiniciar a API.

## 6. Login de verdade (Supabase Auth)
A API não aceita mais os cabeçalhos de teste: cada chamada precisa do token de um usuário logado.

1. No Supabase, abra **Authentication > Users > Add user > Create new user**. Informe e-mail e senha e marque a opção de confirmar o e-mail automaticamente. Copie o **User UID** do usuário criado.
2. No **SQL Editor**, vincule o usuário ao estabelecimento (troque os valores):

       insert into usuarios (id, estabelecimento_id, nome, perfil)
       values ('UID-DO-USUARIO', 'UUID-DO-ESTABELECIMENTO', 'Seu Nome', 'administrador');

   Perfis válidos: `administrador`, `gerente`, `operador`. É essa tabela que define o que cada pessoa pode fazer.
3. Em **Project Settings > API Keys**, copie a chave **publishable** (ou "anon public"). Ela é pública por desenho.
   **Nunca** use a chave `secret` ou `service_role` aqui.
4. Suba a API com as três variáveis:

       $env:DATABASE_URL = "..."
       $env:SUPABASE_URL = "https://yvhqcdmebhfuflwgmhiw.supabase.co"
       $env:SUPABASE_ANON_KEY = "cole-a-chave-publishable"
       python -m uvicorn app.main:app --reload

5. Em `/docs`, rode `POST /auth/entrar` com o e-mail e a senha, copie o `access_token`, clique no botão **Authorize** (cadeado, no topo) e cole o token. Pronto: as demais rotas passam a funcionar como esse usuário. `GET /eu` mostra quem você é.

## 7. Atualizar o banco para a versão 0.4 (migração 002)
1. No **SQL Editor**, cole todo o conteúdo de `migrations/002_gestao.sql` e clique em **Run**. É seguro rodar de novo.
2. (Opcional) Rode também `migrations/003_storage_logos.sql` para criar a pasta pública de logos. Ela só foi testada com um Storage simulado, não no Supabase de verdade; se falhar, o resto do sistema não depende dela.

O que a migração faz: cria os campos de marca e tipo de negócio, o endereço (`slug`) do cardápio, preço/categoria/promoção/disponibilidade nos produtos, as tabelas de pedidos, liga o RLS nelas, libera o tempo real para pedidos e cria o **cadastro automático**.

**Atenção depois de rodar:**
- Os produtos que você já tinha ficam com **preço de venda R$ 0,00** e categoria "Sem categoria". Defina o preço de cada um (`PUT /fichas/{nome}/preco`) antes de abrir o cardápio: produto sem preço não aparece nele.
- Para ver o endereço do seu cardápio: `select nome, slug from estabelecimentos;`
- **Criar usuário pelo painel (Add user) agora cria também um estabelecimento novo** para essa pessoa. Para colocar alguém dentro do SEU negócio, crie o usuário e rode (trocando os valores) e depois apague o estabelecimento que sobrou:

      update usuarios set estabelecimento_id = 'UUID-DO-SEU-ESTABELECIMENTO', perfil = 'operador' where id = 'UID-DA-PESSOA';

  O convite por e-mail feito pela própria API ainda não existe (ele exigiria guardar a chave de administrador no servidor).
- Nunca confie em dados que o próprio usuário pode editar para decidir o estabelecimento ou o perfil dele: o gatilho só usa `raw_app_meta_data` (definido pelo servidor) para isso.

## 8. Produção: variáveis de ambiente
| Variável | Para quê |
|---|---|
| `STOCKPULSE_ENV=producao` | Desliga `/docs`, `/redoc` e `/openapi.json` |
| `CORS_ORIGINS=https://seusite.com.br` | Endereços do seu site que podem chamar a API pelo navegador (vários, separados por vírgula). Sem isso, o navegador bloqueia chamadas de outro endereço |
| `STOCKPULSE_PROXY=1` | Use só se a API estiver atrás de um proxy que define `X-Forwarded-For`; sem isso o limite de pedidos usa o IP do proxy |
| `STOCKPULSE_TZ` | Fuso do negócio (padrão `America/Sao_Paulo`), usado para "hoje" e para o dia da venda |
| `STOCKPULSE_AUTH=cabecalhos` | **Nunca em produção.** Só para testes |

## 9. Cardápio público
1. Preencha a marca e ligue o cardápio: `PUT /estabelecimento` com `{"marca_nome": "...", "whatsapp": "83999990000", "cor_marca": "#ff8a00", "cardapio_publico": true}`.
2. O endereço é `GET /publico/{slug}/cardapio` (sem login) e o cliente faz o pedido em `POST /publico/{slug}/pedidos` com apenas nome, telefone e `[{ficha, quantidade}]`. **O preço vem sempre do banco.** O cardápio nunca devolve custo, margem nem estoque.
3. Há limite de 10 pedidos por hora por IP e por restaurante. Esse limite fica na memória de cada processo: com vários processos ele fica mais frouxo, então use também o limite do seu servidor/proxy.

## 10. Alerta de pedido em tempo real
O banco já publica a tabela `pedidos` no Realtime e só deixa cada pessoa ler os pedidos do próprio estabelecimento. No front, assine as mudanças de `pedidos` com o cliente do Supabase usando o token do usuário logado. Essa assinatura ainda não foi testada de ponta a ponta com o Supabase.

## Testes automatizados
Os testes APAGAM o banco em que rodam. Use apenas um PostgreSQL local de teste, nunca o do Supabase:

    $env:TEST_DATABASE_URL = "postgresql://postgres:SENHA@127.0.0.1:5432/stockpulse_test"
    python -m pytest

Sem essa variável, os testes de banco são ignorados e só os testes de cálculo rodam. Com o banco local, a suíte tem 87 testes.
