# Autenticação do StockPulse: guia de configuração e publicação

## 1. Como funciona
- As senhas ficam **somente no Supabase Auth** (com hash bcrypt). A API e a página nunca guardam, registram nem devolvem senhas.
- A página fala só com a API do StockPulse (`/auth/...`). Nenhuma chave secreta vai para o navegador.
- Cada pedido à API leva o token do usuário; a API pergunta ao Supabase se ele é válido e busca o **perfil e o estabelecimento no banco** (tabela `usuarios`). O perfil nunca vem do que o navegador envia.
- Cadastro público: a pessoa vira administradora **do seu próprio estabelecimento novo** (feito pelo gatilho do banco). Ela não escolhe perfil nem entra em outro estabelecimento. Para colocar a equipe no seu negócio, o administrador usa `POST /equipe/convites`.

## 2. O erro 500 no login: causa e como confirmar
Antes, o login dependia de três coisas e **qualquer falha virava um 500 mudo**: a conexão com o banco (`DATABASE_URL`), a chave do Supabase (`SUPABASE_ANON_KEY`) e a URL do Supabase (`SUPABASE_URL`). A API abria a conexão com o banco *antes* de checar a senha, então um `DATABASE_URL` ausente ou errado derrubava o login com 500.

Não consegui ver os seus logs do Render, então **não posso afirmar qual foi a causa no seu caso**. Agora cada falha vira uma resposta clara e um registro no log. Para descobrir, abra `https://SEU-APP.onrender.com/saude`:

| Resposta | Significa | O que fazer |
|---|---|---|
| `"banco": "sem_configuracao"` | `DATABASE_URL` não definida | Defina no Render |
| `"banco": "falha"` | Banco inacessível | Use a URL do **Session pooler** (a conexão direta do Supabase costuma exigir IPv6, que o Render pode não oferecer); confira a senha (caracteres especiais precisam ser codificados na URL) |
| `"supabase": "nao_configurado"` | Falta `SUPABASE_URL` ou `SUPABASE_ANON_KEY` | Defina no Render |
| `"supabase": "chave_invalida"` | A chave não é deste projeto | Copie de novo a chave **publishable** |
| `"supabase": "inacessivel"` | A API não alcança o Supabase | Confira a `SUPABASE_URL` |
| `"confirmacao_de_email": false` | "Confirm email" está desligado no Supabase | Ligue (item 3) |
| tudo `ok` e o login ainda falha | Veja os logs do Render (linhas `stockpulse`) e me envie, sem senhas | |

`/saude` não mostra nenhum segredo. Em produção só `/docs` fica desligado; `/saude` continua ativo.

## 3. Configuração no painel do Supabase (Authentication)
Os nomes dos menus mudam de vez em quando; procure pelo nome da opção.
1. **URL Configuration:** *Site URL* = endereço do seu site (por exemplo `https://app.seudominio.com.br`). Em *Redirect URLs*, adicione o mesmo endereço. Sem isso, os links dos e-mails não voltam para o seu site.
2. **Email (provedor):** ligue **Confirm email**. Sem isso, qualquer e-mail digitado (inclusive de terceiros) já entra como confirmado.
3. **Política de senha:** exija pelo menos 10 caracteres e letras maiúsculas, minúsculas e números, igual à política da API.
4. **SMTP próprio:** configure um servidor de e-mail (por exemplo Resend, SendGrid ou Amazon SES). O envio padrão do Supabase é para testes e tem limite baixo.
5. **Rate limits e CAPTCHA:** revise os limites de envio de e-mail e de login, e considere ligar a proteção contra robôs (CAPTCHA) no cadastro. A API já aceita o campo `captcha_token`; o formulário da página ainda não o exibe.
6. **Duração do token:** o encerramento da sessão (logout) invalida o token de renovação, mas um token de acesso já emitido continua válido até expirar. Considere reduzir a validade dele.

## 4. Variáveis de ambiente (Render)
| Variável | Obrigatória | Valor |
|---|---|---|
| `DATABASE_URL` | Sim | URL do Session pooler do Supabase |
| `SUPABASE_URL` | Sim | `https://SEU-PROJETO.supabase.co` |
| `SUPABASE_ANON_KEY` | Sim | Chave **publishable** (pública por desenho) |
| `CORS_ORIGINS` | Sim | O endereço exato do seu site |
| `AUTH_REDIRECT_URL` | Recomendada | Para onde os links dos e-mails levam (o seu site) |
| `STOCKPULSE_ENV` | Sim | `producao` |
| `STOCKPULSE_PROXY` | Se houver proxy | `1` |
| `SUPABASE_SERVICE_ROLE_KEY` | Só para convites | Chave **secreta**. Fica só no servidor. **Nunca** na página, no GitHub ou em conversas |
| `STOCKPULSE_AUTH` | **Nunca em produção** | Só para testes locais |

## 5. Banco de dados
**Esta etapa não exigiu nenhuma migração nova.** O gatilho de cadastro e as tabelas já vieram na `002_gestao.sql`. Nenhuma tabela, usuário ou dado foi alterado ou apagado.

## 6. Publicar
1. **API (Render):** suba o zip atualizado no seu repositório e faça o deploy (comando de início `uvicorn app.main:app --host 0.0.0.0 --port $PORT`). Configure as variáveis do item 4 e abra `/saude`.
2. **Página:** no `stockpulse.html`, preencha **uma linha**: `<meta name="api-url" content="https://SEU-APP.onrender.com">`. Hospede o arquivo como site estático (Render Static Site, Netlify etc.) em um endereço `https`. Abrir o arquivo direto do computador (`file://`) não funciona, porque o navegador bloqueia as chamadas (CORS).
3. Coloque esse mesmo endereço em `CORS_ORIGINS` e em *Site URL* do Supabase.
4. Entre com o usuário que você já tem. Ele continua funcionando, com o mesmo perfil.

## 7. Teste manual (roteiro)
Cadastro de usuário novo · e-mail de confirmação chegando · login válido · senha incorreta (mensagem genérica) · e-mail inválido · cadastro com e-mail já existente (mesma mensagem, sem revelar) · 6 senhas erradas seguidas (bloqueio com aviso de espera) · "Esqueci minha senha" e o link do e-mail · recarregar a página (a sessão deve continuar) · Sair · abrir o sistema sem login (deve pedir login) · entrar como administrador, gerente e operador e conferir os menus.

## 8. Testes automatizados
- API: `python -m pytest` (138 testes, com PostgreSQL **local**).
- Página: `tests_front/` (49 cenários de autenticação e uma regressão de todas as telas).

## 9. Limites e pendências (importante)
- **Sessão no navegador:** o token de renovação fica no `localStorage` (ou `sessionStorage`). Se a página tiver uma falha de XSS, ele pode ser roubado. O ideal futuro é um cookie `httpOnly`, que exige que o site e a API fiquem no mesmo domínio.
- **Limite de tentativas** fica na memória da API: vale para uma instância (o Render, com 1 instância, serve). Com várias, use também um WAF/proxy. O bloqueio por e-mail pode ser usado para atrapalhar o login de alguém (o mesmo vale para o do próprio Supabase); por isso ele é por IP + e-mail, com limites mais altos para o ataque em larga escala.
- **E-mail já cadastrado:** por segurança a tela **não avisa**; a pessoa só descobre que já tem conta pela ausência do e-mail de confirmação (ou pela recuperação de senha).
- **Convites (`/equipe/convites`)** usam a API de administração do Supabase e foram testados só com respostas simuladas, **não com o Supabase real**. Teste com um e-mail seu antes de usar.
- **Dados da tela de demonstração:** os dados de exemplo das outras telas ainda ficam no `localStorage` do navegador, mesmo depois de sair. Em um computador compartilhado, a pessoa seguinte vê esses dados locais. Isso só se resolve ligando as telas ao banco (próximas etapas).
- Autenticação em dois fatores (MFA) não foi feita.
