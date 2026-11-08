-- StockPulse · migração 002: gestão completa
-- Rode UMA vez no SQL Editor do Supabase, depois do schema.sql. É seguro rodar de novo (idempotente).
-- Adiciona: dados do negócio e marca, preço/categoria nos produtos, pedidos, cadastro automático
-- de novo cliente (gatilho no login), tempo real para pedidos e RLS nas tabelas novas.

-- 1) Estabelecimento: tipo de negócio, marca e cardápio público
alter table estabelecimentos
  add column if not exists slug text,
  add column if not exists tipo_negocio text not null default 'hamburgueria'
      check (tipo_negocio in ('hamburgueria','restaurante','acai','bar','pizzaria','outro')),
  add column if not exists marca_nome text,
  add column if not exists slogan text,
  add column if not exists whatsapp text check (whatsapp is null or whatsapp ~ '^[0-9]{10,13}$'),
  add column if not exists cor_marca text not null default '#7fee64' check (cor_marca ~ '^#[0-9a-fA-F]{6}$'),
  add column if not exists logo_url text check (logo_url is null or logo_url ~ '^https://'),
  add column if not exists cardapio_publico boolean not null default false,
  add column if not exists ultimo_pedido integer not null default 0;

create or replace function public.gerar_slug(nome text, uid uuid) returns text
language sql immutable as $$
  select coalesce(nullif(trim(both '-' from regexp_replace(
           translate(lower(coalesce(nome, '')), 'áàâãäéèêëíìîïóòôõöúùûüçñ', 'aaaaaeeeeiiiiooooouuuucn'),
           '[^a-z0-9]+', '-', 'g')), ''), 'negocio') || '-' || substr(uid::text, 1, 4)
$$;

update estabelecimentos set slug = public.gerar_slug(nome, id) where slug is null;
create unique index if not exists estabelecimentos_slug_key on estabelecimentos (slug) where slug is not null;

-- 2) Produtos de venda (fichas técnicas): categoria, preço, promoção e disponibilidade
alter table fichas_tecnicas
  add column if not exists categoria text not null default 'Sem categoria',
  add column if not exists preco_venda numeric(12,2) not null default 0 check (preco_venda >= 0),
  add column if not exists preco_promocional numeric(12,2)
      check (preco_promocional is null or (preco_promocional > 0 and preco_promocional < preco_venda)),
  add column if not exists ativo boolean not null default true;

-- 3) Pedidos
create table if not exists pedidos (
  id                 uuid primary key default gen_random_uuid(),
  estabelecimento_id uuid not null references estabelecimentos(id) on delete cascade,
  numero             integer not null,
  canal              text not null check (canal in ('Balcão','Delivery','WhatsApp','Mesa','Cardápio')),
  status             text not null default 'novo' check (status in ('novo','preparo','pronto','entregue','cancelado')),
  cliente_nome       text not null,
  cliente_telefone   text,
  pagamento          text not null default 'A combinar',
  observacao         text,
  total              numeric(14,2) not null default 0 check (total >= 0),
  visto              boolean not null default false,
  criado_em          timestamptz not null default now(),
  concluido_em       timestamptz,
  unique (estabelecimento_id, numero)
);
create index if not exists pedidos_estab_status on pedidos (estabelecimento_id, status, criado_em);

create table if not exists itens_pedido (
  id          uuid primary key default gen_random_uuid(),
  pedido_id   uuid not null references pedidos(id) on delete cascade,
  ficha_id    uuid references fichas_tecnicas(id) on delete set null,
  nome        text not null,                                  -- fotografia do nome na hora do pedido
  quantidade  numeric(12,3) not null check (quantidade > 0),
  preco_unit  numeric(12,2) not null check (preco_unit > 0)   -- preço definido pelo servidor, nunca pelo cliente
);
create index if not exists itens_pedido_pedido on itens_pedido (pedido_id);

alter table vendas add column if not exists pedido_id uuid references pedidos(id) on delete set null;

-- 4) Segurança: RLS nas tabelas novas (a chave pública não lê nem grava nada pela Data API)
alter table pedidos enable row level security;
alter table itens_pedido enable row level security;

-- Quem está logado enxerga só os pedidos do próprio estabelecimento (necessário para o tempo real)
drop policy if exists pedidos_leitura on pedidos;
create policy pedidos_leitura on pedidos for select
  using (estabelecimento_id = (select estabelecimento_id from usuarios where id = auth.uid()));

-- 5) Tempo real: avisa o painel quando um pedido novo chega
do $$ begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime')
     and not exists (select 1 from pg_publication_tables
                     where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'pedidos') then
    alter publication supabase_realtime add table public.pedidos;
  end if;
end $$;

-- 6) Cadastro automático de novo cliente
-- Quem se cadastra (sem app_meta) ganha um estabelecimento próprio e vira administrador.
-- Convites feitos pelo SERVIDOR trazem raw_app_meta_data com estabelecimento_id e perfil: aí a pessoa
-- entra no estabelecimento indicado. Só raw_app_meta_data vale para isso, pois raw_user_meta_data
-- é editável pelo próprio usuário e nunca deve decidir a que estabelecimento alguém pertence.
create or replace function public.novo_usuario() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  est  uuid;
  meta jsonb := coalesce(new.raw_user_meta_data, '{}'::jsonb);
  app  jsonb := coalesce(new.raw_app_meta_data, '{}'::jsonb);
  perf text;
  nome_neg text;
  tipo text;
  nome_usuario text;
begin
  nome_usuario := left(coalesce(nullif(trim(app->>'nome'), ''), nullif(trim(meta->>'nome'), ''),
                                nullif(split_part(coalesce(new.email, ''), '@', 1), ''), 'Usuário'), 80);
  if app ? 'estabelecimento_id' then
    perf := coalesce(app->>'perfil', 'operador');
    if perf not in ('administrador', 'gerente', 'operador') then perf := 'operador'; end if;
    insert into usuarios (id, estabelecimento_id, nome, perfil)
    values (new.id, (app->>'estabelecimento_id')::uuid, nome_usuario, perf::perfil);
  else
    nome_neg := left(coalesce(nullif(trim(meta->>'nome_negocio'), ''), 'Meu negócio'), 80);
    tipo := coalesce(meta->>'tipo_negocio', 'hamburgueria');
    if tipo not in ('hamburgueria', 'restaurante', 'acai', 'bar', 'pizzaria', 'outro') then tipo := 'outro'; end if;
    insert into estabelecimentos (nome, tipo_negocio) values (nome_neg, tipo) returning id into est;
    update estabelecimentos set slug = public.gerar_slug(nome_neg, est) where id = est;
    insert into usuarios (id, estabelecimento_id, nome, perfil) values (new.id, est, nome_usuario, 'administrador');
  end if;
  return new;
end $$;

drop trigger if exists ao_criar_usuario on auth.users;
create trigger ao_criar_usuario after insert on auth.users
  for each row execute function public.novo_usuario();
