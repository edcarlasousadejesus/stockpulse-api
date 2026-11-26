-- StockPulse · migração 005: personalização IA, bloqueios WhatsApp, carrinhos, entrega/motoboys, cupons e fidelidade
-- Idempotente: segura para rodar mais de uma vez no SQL Editor do Supabase.

alter table estabelecimentos
  add column if not exists assistente_ia jsonb not null default '{"nome":"Atendente StockPulse","voz_tts":"nova","formalidade":"equilibrado","estilo":"","tempo_recuperacao_min":15}'::jsonb,
  add column if not exists whatsapp_bloqueios jsonb not null default '[]'::jsonb,
  add column if not exists entrega_config jsonb not null default '{"bairros":[],"raios":[]}'::jsonb,
  add column if not exists cashback_pct numeric(5,2) not null default 5 check (cashback_pct between 0 and 100);

create table if not exists carrinhos_abandonados (
  id                 uuid primary key default gen_random_uuid(),
  estabelecimento_id uuid not null references estabelecimentos(id) on delete cascade,
  telefone           text not null,
  cliente_nome       text not null default 'Cliente',
  itens              jsonb not null default '[]'::jsonb,
  total              numeric(14,2) not null default 0,
  canal              text not null default 'WhatsApp',
  status             text not null default 'aberto' check (status in ('aberto','lembrado','recuperado')),
  atualizado_em      timestamptz not null default now(),
  lembrado_em        timestamptz
);
create index if not exists carrinhos_estab_status on carrinhos_abandonados (estabelecimento_id, status, atualizado_em);

create table if not exists motoboys (
  id                 uuid primary key default gen_random_uuid(),
  estabelecimento_id uuid not null references estabelecimentos(id) on delete cascade,
  nome               text not null,
  telefone           text not null,
  veiculo            text not null default 'Moto',
  ativo              boolean not null default true,
  criado_em          timestamptz not null default now()
);

create table if not exists cupons (
  id                 uuid primary key default gen_random_uuid(),
  estabelecimento_id uuid not null references estabelecimentos(id) on delete cascade,
  codigo             text not null,
  tipo               text not null check (tipo in ('pct','fixo')),
  valor              numeric(12,2) not null check (valor > 0),
  limite_uso         integer not null default 100 check (limite_uso >= 1),
  usos               integer not null default 0 check (usos >= 0),
  ativo              boolean not null default true,
  criado_em          timestamptz not null default now(),
  unique (estabelecimento_id, codigo)
);

create table if not exists fidelidade_clientes (
  estabelecimento_id uuid not null references estabelecimentos(id) on delete cascade,
  telefone           text not null,
  pontos             integer not null default 0 check (pontos >= 0),
  cashback_saldo     numeric(12,2) not null default 0 check (cashback_saldo >= 0),
  atualizado_em      timestamptz not null default now(),
  primary key (estabelecimento_id, telefone)
);

do $$ declare t text; begin
  for t in select unnest(array['carrinhos_abandonados','motoboys','cupons','fidelidade_clientes']) loop
    execute format('alter table public.%I enable row level security', t);
  end loop;
end $$;
