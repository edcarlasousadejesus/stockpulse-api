-- StockPulse · migração 003 (OPCIONAL): pasta pública de logos no Supabase Storage
-- Cada estabelecimento só grava na própria pasta (logos/<id-do-estabelecimento>/arquivo.png);
-- a leitura é pública, porque o logo aparece no cardápio do cliente.
-- Atenção: esta migração só foi testada com um esquema "storage" simulado, não no Supabase de verdade.
-- Se algo falhar ao rodar, o restante do sistema continua funcionando (o logo é só um endereço https).

insert into storage.buckets (id, name, public) values ('logos', 'logos', true)
on conflict (id) do nothing;

drop policy if exists logos_leitura on storage.objects;
create policy logos_leitura on storage.objects for select using (bucket_id = 'logos');

drop policy if exists logos_envio on storage.objects;
create policy logos_envio on storage.objects for insert to authenticated
  with check (bucket_id = 'logos'
              and (storage.foldername(name))[1] = (select estabelecimento_id::text from public.usuarios where id = auth.uid()));

drop policy if exists logos_troca on storage.objects;
create policy logos_troca on storage.objects for update to authenticated
  using (bucket_id = 'logos'
         and (storage.foldername(name))[1] = (select estabelecimento_id::text from public.usuarios where id = auth.uid()));

drop policy if exists logos_remocao on storage.objects;
create policy logos_remocao on storage.objects for delete to authenticated
  using (bucket_id = 'logos'
         and (storage.foldername(name))[1] = (select estabelecimento_id::text from public.usuarios where id = auth.uid()));
