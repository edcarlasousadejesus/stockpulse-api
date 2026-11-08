"""Os testes da API precisam de um PostgreSQL LOCAL de teste (nunca o do Supabase):

    $env:TEST_DATABASE_URL = "postgresql://postgres:senha@127.0.0.1:5432/stockpulse_test"

Eles APAGAM e recriam o schema public desse banco a cada execução.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest

URL = os.environ.get("TEST_DATABASE_URL")
if URL:
    os.environ["DATABASE_URL"] = URL
os.environ["STOCKPULSE_AUTH"] = "cabecalhos"  # testes de rota usam cabeçalhos; test_auth.py testa o token

EST_A = "00000000-0000-0000-0000-00000000000a"
EST_B = "00000000-0000-0000-0000-00000000000b"
U_GER, U_OPE, U_B, U_SEM = (f"10000000-0000-0000-0000-00000000000{i}" for i in "1234")
RAIZ = Path(__file__).resolve().parent.parent
SCHEMA = RAIZ / "schema.sql"
AUTH_STUB = """
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
end $$;
create schema auth;
create table auth.users (id uuid primary key, email text, raw_user_meta_data jsonb not null default '{}', raw_app_meta_data jsonb not null default '{}');
create function auth.uid() returns uuid language sql stable as $$ select null::uuid $$;
create schema storage;
create table storage.buckets (id text primary key, name text, public boolean);
create table storage.objects (id uuid primary key default gen_random_uuid(), bucket_id text, name text);
alter table storage.objects enable row level security;
create function storage.foldername(name text) returns text[] language sql immutable as $$ select (string_to_array(name, '/'))[1:array_length(string_to_array(name, '/'), 1) - 1] $$;
"""


@pytest.fixture(scope="session")
def banco():
    if not URL:
        pytest.skip("defina TEST_DATABASE_URL apontando para um PostgreSQL local de teste")
    if urlparse(URL).hostname not in ("localhost", "127.0.0.1") and os.environ.get("ALLOW_REMOTE_TESTS") != "1":
        pytest.skip("recusado: o banco de teste não é local (os testes apagam todos os dados)")
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("drop schema if exists public cascade; create schema public; drop schema if exists auth cascade; drop schema if exists storage cascade")
        c.execute(AUTH_STUB)
        c.execute(SCHEMA.read_text(encoding="utf-8"))
        for m in sorted((RAIZ / "migrations").glob("*.sql")):
            c.execute(m.read_text(encoding="utf-8"))
    yield


@pytest.fixture
def limpo(banco):
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("truncate estabelecimentos, auth.users restart identity cascade")
        c.execute("insert into estabelecimentos (id, nome, slug) values (%s, 'Loja A', 'loja-a'), (%s, 'Loja B', 'loja-b')", (EST_A, EST_B))
        def novo(uid, email, est=None, perfil=None, nome=None):
            app = '{}' if not est else json.dumps({"estabelecimento_id": est, "perfil": perfil, "nome": nome})
            c.execute("insert into auth.users (id, email, raw_app_meta_data) values (%s, %s, %s::jsonb)", (uid, email, app))
        novo(U_GER, "ana@x.com", EST_A, "gerente", "Ana")
        novo(U_OPE, "beto@x.com", EST_A, "operador", "Beto")
        novo(U_B, "caio@x.com", EST_B, "gerente", "Caio")
        novo(U_SEM, "sem@x.com")
        c.execute("delete from usuarios where id = %s", (U_SEM,))  # autenticado, mas sem vínculo
