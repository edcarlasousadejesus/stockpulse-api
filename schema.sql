-- StockPulse · esquema PostgreSQL (compatível com Supabase)
-- Todo registro operacional pertence a um estabelecimento.

create type perfil as enum ('administrador', 'gerente', 'operador');
create type tipo_mov as enum ('entrada', 'saida', 'contagem', 'perda');

create table estabelecimentos (
  id            uuid primary key default gen_random_uuid(),
  nome          text not null,
  cnpj          text,
  meta_cmv_pct  numeric(5,2) not null default 28 check (meta_cmv_pct between 0 and 100),
  criado_em     timestamptz not null default now()
);

create table usuarios (
  id                 uuid primary key references auth.users(id),
  estabelecimento_id uuid not null references estabelecimentos(id),
  nome               text not null,
  perfil             perfil not null default 'operador'
);

create table produtos (
  id                 uuid primary key default gen_random_uuid(),
  estabelecimento_id uuid not null references estabelecimentos(id),
  nome               text not null,
  categoria          text,
  unidade            text not null,            -- kg, un, l
  custo_medio        numeric(12,4) not null default 0 check (custo_medio >= 0),
  ativo              boolean not null default true,
  unique (estabelecimento_id, nome)
);

create table fornecedores (
  id                 uuid primary key default gen_random_uuid(),
  estabelecimento_id uuid not null references estabelecimentos(id),
  nome               text not null,
  unique (estabelecimento_id, nome)
);

create table compras (
  id                 uuid primary key default gen_random_uuid(),
  estabelecimento_id uuid not null references estabelecimentos(id),
  fornecedor_id      uuid references fornecedores(id),
  data               date not null,
  nota_fiscal        text,
  criado_em          timestamptz not null default now()
);

create table itens_compra (
  id          uuid primary key default gen_random_uuid(),
  compra_id   uuid not null references compras(id) on delete cascade,
  produto_id  uuid not null references produtos(id),
  quantidade  numeric(14,4) not null check (quantidade > 0),
  custo_unit  numeric(12,4) not null check (custo_unit >= 0)
);

-- Livro de movimentações: saldo = soma. Nunca apague; corrija com novo lançamento.
create table movimentacoes_estoque (
  id                 uuid primary key default gen_random_uuid(),
  estabelecimento_id uuid not null references estabelecimentos(id),
  produto_id         uuid not null references produtos(id),
  tipo               tipo_mov not null,
  quantidade         numeric(14,4) not null,    -- positiva entra, negativa sai
  custo_unit         numeric(12,4),
  data               date not null,
  origem             text,                      -- compra, venda, perda, inventario
  criado_por         uuid references usuarios(id),
  criado_em          timestamptz not null default now()
);
create index on movimentacoes_estoque (estabelecimento_id, produto_id, data);

create table fichas_tecnicas (
  id                 uuid primary key default gen_random_uuid(),
  estabelecimento_id uuid not null references estabelecimentos(id),
  nome               text not null,
  outros_custos      numeric(12,4) not null default 0,
  unique (estabelecimento_id, nome)
);

create table itens_ficha (
  ficha_id    uuid not null references fichas_tecnicas(id) on delete cascade,
  produto_id  uuid not null references produtos(id),
  quantidade  numeric(14,4) not null check (quantidade > 0),
  primary key (ficha_id, produto_id)
);

create table vendas (
  id                 uuid primary key default gen_random_uuid(),
  estabelecimento_id uuid not null references estabelecimentos(id),
  ficha_id           uuid not null references fichas_tecnicas(id),
  data               date not null,
  quantidade         numeric(12,3) not null check (quantidade > 0),
  preco_unit         numeric(12,2) not null check (preco_unit > 0),
  custo_teorico      numeric(14,2) not null default 0,   -- fotografia do custo da ficha na hora da venda
  criado_em          timestamptz not null default now()
);

create table perdas (
  id                 uuid primary key default gen_random_uuid(),
  estabelecimento_id uuid not null references estabelecimentos(id),
  produto_id         uuid not null references produtos(id),
  data               date not null,
  quantidade         numeric(14,4) not null check (quantidade > 0),
  motivo             text not null,             -- vencimento, descarte, quebra
  valor              numeric(14,2) not null default 0,   -- fotografia do custo na hora da perda
  criado_em          timestamptz not null default now()
);

-- Fotografia imutável do dia. Reabrir = novo registro com status 'reaberto'.
create table fechamentos_diarios (
  id                 uuid primary key default gen_random_uuid(),
  estabelecimento_id uuid not null references estabelecimentos(id),
  data               date not null,
  faturamento        numeric(14,2) not null check (faturamento > 0),
  estoque_inicial    numeric(14,2) not null,
  compras_liquidas   numeric(14,2) not null,
  estoque_final      numeric(14,2) not null,
  cmv                numeric(14,2) generated always as (estoque_inicial + compras_liquidas - estoque_final) stored,
  custo_teorico      numeric(14,2),
  perdas             numeric(14,2) not null default 0,
  status             text not null default 'fechado' check (status in ('fechado','reaberto')),
  fechado_por        uuid references usuarios(id),
  fechado_por_nome   text,
  fechado_em         timestamptz not null default now()
);
create unique index on fechamentos_diarios (estabelecimento_id, data) where status = 'fechado';

create table auditoria (
  id                 bigint generated always as identity primary key,
  estabelecimento_id uuid not null references estabelecimentos(id),
  usuario_id         uuid references usuarios(id),
  usuario_nome       text,
  perfil             text,
  acao               text not null,
  motivo             text,
  antes              jsonb,
  depois             jsonb,
  criado_em          timestamptz not null default now()
);

-- Segurança no Supabase: ativa RLS em TODAS as tabelas. Sem política, a chave pública (anon)
-- não lê nem grava nada pela Data API. A API do StockPulse conecta com o papel do banco
-- (postgres), que ignora RLS, e aplica o isolamento por estabelecimento no código.
do $$ declare t text; begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table public.%I enable row level security', t);
  end loop;
end $$;

-- Política de exemplo para quando o front passar a consultar o banco com o login do usuário
create policy isolamento on fechamentos_diarios
  using (estabelecimento_id = (select estabelecimento_id from usuarios where id = auth.uid()));

-- Saldo atual por produto
create view saldo_estoque as
select estabelecimento_id, produto_id, sum(quantidade) as saldo
from movimentacoes_estoque group by 1, 2;

-- CMV diário com percentual e comparação com a meta
create view cmv_diario as
select f.estabelecimento_id, f.data, f.faturamento, f.cmv,
       round(f.cmv / f.faturamento * 100, 1) as cmv_pct,
       e.meta_cmv_pct,
       round(f.cmv / f.faturamento * 100, 1) > e.meta_cmv_pct as acima_da_meta,
       f.faturamento - f.cmv as margem_bruta
from fechamentos_diarios f
join estabelecimentos e on e.id = f.estabelecimento_id
where f.status = 'fechado';
