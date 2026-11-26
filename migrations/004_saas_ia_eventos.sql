-- StockPulse · migração 004: preparação SaaS, uso de IA, eventos de pedido e tempo de preparo
-- Idempotente: segura para rodar mais de uma vez no SQL Editor do Supabase.

-- 1) Plano e controle de IA por estabelecimento (base para faturamento SaaS)
alter table estabelecimentos
  add column if not exists plano text not null default 'starter'
      check (plano in ('free', 'starter', 'pro', 'enterprise')),
  add column if not exists ia_ativo boolean not null default true,
  add column if not exists tempo_preparo_padrao_min integer not null default 20 check (tempo_preparo_padrao_min between 1 and 180);

-- 2) Timestamps de etapas e tempo de preparo nos pedidos
alter table pedidos
  add column if not exists etapas jsonb not null default '{}'::jsonb,
  add column if not exists preparo_em timestamptz,
  add column if not exists pronto_em timestamptz,
  add column if not exists tempo_preparo_min integer;

-- 3) Registro de consumo de IA por estabelecimento e usuário (para limites de plano e billing)
create table if not exists uso_ia (
  id                 bigint generated always as identity primary key,
  estabelecimento_id uuid not null references estabelecimentos(id) on delete cascade,
  usuario_id         uuid references usuarios(id) on delete set null,
  recurso            text not null check (recurso in ('gerar_arte', 'whatsapp_stt', 'whatsapp_nlu', 'whatsapp_tts')),
  provedor           text not null,
  custo_unidades     integer not null default 1 check (custo_unidades >= 1),
  metadados          jsonb not null default '{}'::jsonb,
  criado_em          timestamptz not null default now()
);
create index if not exists uso_ia_estab_dia on uso_ia (estabelecimento_id, recurso, criado_em desc);

alter table uso_ia enable row level security;
drop policy if exists uso_ia_leitura on uso_ia;
create policy uso_ia_leitura on uso_ia for select
  using (estabelecimento_id = (select estabelecimento_id from usuarios where id = auth.uid()));
