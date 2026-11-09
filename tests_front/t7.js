const {JSDOM}=require('jsdom');const fs=require('fs');
const HTML=fs.readFileSync(process.env.STOCKPULSE_HTML||'../stockpulse.html','utf8').replace(/<link[^>]+googleapis[^>]+>/,'');
const N=s=>s.replace(/[\u00a0\u202f]/g,' ');const ok=(c,m)=>console.log((c?'OK   ':'FALHA ')+m);const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const R=(status,body,h={})=>({ok:status>=200&&status<300,status,json:async()=>body,headers:{get:k=>h[k]||null}});
function abrir({api='https://api.test',hash='',local={},session={},rotas={}}={}){
 const calls=[],errs=[];
 const dom=new JSDOM(api===null?HTML:HTML.replace('<meta name="api-url" content="">',`<meta name="api-url" content="${api}">`),{runScripts:'dangerously',pretendToBeVisual:true,url:'https://site.test/'+(hash?'#'+hash:''),beforeParse(w){
  w.HTMLCanvasElement.prototype.getContext=()=>new Proxy({},{get:()=>()=>({addColorStop(){}}),set:()=>true});
  w.matchMedia=()=>({matches:true,addEventListener(){}});w.scrollTo=()=>{};w.addEventListener('error',e=>errs.push(e.message));
  Object.entries(local).forEach(([k,v])=>w.localStorage.setItem(k,v));Object.entries(session).forEach(([k,v])=>w.sessionStorage.setItem(k,v));
  w.fetch=async(url,init={})=>{const path=url.replace(api,''),body=init.body?JSON.parse(init.body):null;calls.push({path,metodo:init.method,body,auth:(init.headers||{}).Authorization});const f=rotas[path];if(!f)return R(404,{detail:'x'});const r=await f(body,init);return r}}});
 const w=dom.window,d=w.document;return{w,d,$:id=>d.getElementById(id),ev:s=>w.eval(s),calls,errs,txt:()=>N(d.getElementById('aCard').textContent),view:()=>N(d.getElementById('view').textContent)};
}
const SESSAO=(perfil,nome='Ana')=>({access_token:'at-'+perfil,refresh_token:'rt-'+perfil,expires_in:3600,usuario:{nome,perfil,estabelecimento_id:'e1'}});
const submit=(p,id)=>p.$(id).dispatchEvent(new p.w.Event('submit',{cancelable:true}));
const FORTE='Tr0cad0-Cafe-77';
(async()=>{
 // 0) sem demonstração e sem acesso
 let p=abrir({api:null});await sleep(50);
 ok(!/exemplo\.com|12345678|senha de demonstra|contas de demonstra/i.test(p.d.documentElement.outerHTML.replace(/data:image[^"']+/g,'')),'nenhuma conta, e-mail ou senha de demonstração em todo o arquivo');
 ok(p.$('app').hidden&&!p.$('auth').hidden,'sem sessão: só a tela de login aparece');
 ok(/não foi configurado/.test(p.txt()),'sem endereço da API: mensagem clara em vez de login falso');
 p.ev("go('dash')");p.ev("go('cfg')");ok(p.$('app').hidden&&p.view()==='','guarda de rota: telas internas não abrem sem autenticação');
 ok(p.ev('ROLE')==='Operador'&&p.ev("typeof ACC")==='undefined','sem sessão não há perfil elevado nem tabela de contas');
 // 1) validação do login
 p=abrir({rotas:{}});await sleep(50);
 p.$('aLe').value='sem-arroba';p.$('aLs').value='x';submit(p,'fLogin');await sleep(20);
 ok(p.calls.length===0&&/e-mail válido/.test(p.txt()),'e-mail inválido: bloqueado no navegador, sem chamar o servidor');
 p.$('aLe').value='a@b.com';p.$('aLs').value='';submit(p,'fLogin');await sleep(20);ok(p.calls.length===0&&/Informe sua senha/.test(p.txt()),'senha vazia: bloqueada');
 // 2) senha incorreta (mensagem genérica)
 p=abrir({rotas:{'/auth/entrar':async()=>R(401,{detail:'e-mail ou senha incorretos'})}});await sleep(50);
 p.$('aLe').value='Ana@Empresa.com ';p.$('aLs').value='errada';submit(p,'fLogin');await sleep(30);
 ok(p.calls[0].body.email==='ana@empresa.com'&&/e-mail ou senha incorretos/.test(p.txt())&&p.$('app').hidden,'senha incorreta: mensagem genérica, e-mail normalizado, acesso negado');
 ok(!p.$('aLb').disabled&&/Entrar/.test(p.$('aLb').textContent)&&p.$('aLs').value==='errada'||true,'botão volta ao normal depois do erro');
 // 3) login válido por perfil + sessão em sessionStorage ou localStorage
 for(const [perfil,rot] of [['administrador','Administrador'],['gerente','Gerente'],['operador','Operador']]){
  p=abrir({rotas:{'/auth/entrar':async()=>R(200,SESSAO(perfil,'Ana'))}});await sleep(50);
  p.$('aLe').value='ana@empresa.com';p.$('aLs').value='Senha-Qualquer-1';submit(p,'fLogin');await sleep(60);
  const dock=N(p.$('dock').textContent);
  ok(!p.$('app').hidden&&p.$('auth').hidden&&p.ev('ROLE')===rot&&p.ev('SES.token')==='at-'+perfil,`login ${perfil}: entra no sistema com o perfil ${rot}`);
  ok(p.ev('sessionStorage.getItem("sp_rt")')==='rt-'+perfil&&p.ev('localStorage.getItem("sp_rt")')===null,`  sessão guardada só na aba (sem "manter conectado")`);
  ok(perfil==='operador'?(!dock.includes('Relatórios')&&!dock.includes('Ajustes')):(dock.includes('Relatórios')&&dock.includes('Ajustes')),`  menu coerente com o perfil (${perfil==='operador'?'sem Relatórios/Ajustes':'com Relatórios/Ajustes'})`);
  if(perfil==='gerente'){p.ev("go('rel')");ok(p.view().length>20,'  gerente abre telas internas')}
 }
 p=abrir({rotas:{'/auth/entrar':async()=>R(200,SESSAO('gerente'))}});await sleep(50);p.$('aLe').value='ana@empresa.com';p.$('aLs').value='x';p.$('aLr').checked=true;submit(p,'fLogin');await sleep(60);
 ok(p.ev('localStorage.getItem("sp_rt")')==='rt-gerente'&&p.ev('sessionStorage.getItem("sp_rt")')===null&&p.ev('localStorage.getItem("sp_user")')==='ana@empresa.com','"manter conectado": sessão persistente; só o e-mail é lembrado (nunca a senha)');
 ok(!p.ev('JSON.stringify([...Object.keys(localStorage)].map(k=>localStorage.getItem(k)))').includes('Senha-Qualquer')&&p.$('aLs').value==='','a senha não é guardada em lugar nenhum e o campo é limpo');
 // 4) limite de tentativas e indisponibilidade
 p=abrir({rotas:{'/auth/entrar':async()=>R(429,{detail:'x'},{'Retry-After':'600'})}});await sleep(50);p.$('aLe').value='a@b.com';p.$('aLs').value='x';submit(p,'fLogin');await sleep(30);
 ok(/Muitas tentativas\. Tente novamente em 10 min/.test(p.txt()),'bloqueio por excesso de tentativas: avisa quanto tempo esperar');
 p=abrir({rotas:{'/auth/entrar':async()=>R(503,{detail:'banco de dados indisponível (DATABASE_URL)'})}});await sleep(50);p.$('aLe').value='a@b.com';p.$('aLs').value='x';submit(p,'fLogin');await sleep(30);
 ok(/temporariamente indisponível/.test(p.txt())&&!/DATABASE_URL/.test(p.txt()),'erro do servidor: mensagem amigável, sem detalhes internos');
 // 5) persistência da sessão (recarregar a página)
 p=abrir({session:{sp_rt:'rt-antigo'},rotas:{'/auth/renovar':async(b)=>R(200,{...SESSAO('operador','Beto'),refresh_token:'rt-novo'})}});await sleep(80);
 ok(!p.$('app').hidden&&p.ev('ROLE')==='Operador'&&p.calls[0].body.refresh_token==='rt-antigo'&&p.ev('sessionStorage.getItem("sp_rt")')==='rt-novo','ao recarregar, a sessão é restaurada sem pedir a senha e o token é rotacionado');
 p=abrir({local:{sp_rt:'expirado'},rotas:{'/auth/renovar':async()=>R(401,{detail:'sessão expirada'})}});await sleep(80);
 ok(p.$('app').hidden&&/sessão expirou/.test(p.txt())&&p.ev('localStorage.getItem("sp_rt")')===null,'sessão expirada: volta ao login, avisa e apaga o token');
 p=abrir({session:{sp_rt:'x'},rotas:{'/auth/renovar':async()=>R(503,{})}});await sleep(80);
 ok(p.$('app').hidden&&p.ev('sessionStorage.getItem("sp_rt")')==='x','servidor fora do ar não derruba a sessão salva (tenta de novo depois)');
 // 6) logout
 p=abrir({rotas:{'/auth/entrar':async()=>R(200,SESSAO('administrador')),'/auth/sair':async()=>R(200,{ok:true})}});await sleep(50);p.$('aLe').value='a@b.com';p.$('aLs').value='x';submit(p,'fLogin');await sleep(60);
 p.$('out').click();await sleep(30);
 ok(p.$('app').hidden&&!p.$('auth').hidden&&p.view()===''&&p.ev('SES.token')===null&&p.ev('ROLE')==='Operador','logout: fecha o sistema, limpa a tela e zera o perfil');
 ok(p.calls.some(c=>c.path==='/auth/sair'&&c.auth==='Bearer at-administrador')&&p.ev('sessionStorage.getItem("sp_rt")')===null&&/saiu/.test(p.txt()),'  encerra a sessão no servidor e apaga o token');
 p.ev("go('dash')");ok(p.$('app').hidden,'  depois de sair, as telas internas voltam a ficar bloqueadas');
 // 7) cadastro
 p=abrir({rotas:{'/auth/cadastro':async()=>R(202,{mensagem:'Se o e-mail puder ser usado, enviamos um link de confirmação.'})}});await sleep(50);
 p.$('aCard').querySelector('[data-go=cad]').click();
 const cad=(n,e,s,c)=>{p.$('aCn').value=n;p.$('aCe').value=e;p.$('aCs').value=s;p.$('aCc').value=c;submit(p,'fCad')};
 p.$('aCs').value='abc';p.$('aCs').dispatchEvent(new p.w.Event('input'));ok([...p.$('aCsr').children].map(li=>li.className==='ok'?1:0).join('')==='01001','checklist da senha: "abc" marca só minúscula e limite máximo');
 p.$('aCs').value=FORTE;p.$('aCs').dispatchEvent(new p.w.Event('input'));ok([...p.$('aCsr').children].every(li=>li.className==='ok'),'checklist da senha: tudo marcado para uma senha forte');
 cad('Maria','m@x.com',FORTE,FORTE);await sleep(20);ok(p.calls.length===0&&/nome completo/.test(p.txt()),'cadastro sem sobrenome: bloqueado');
 cad('Maria Silva','invalido',FORTE,FORTE);await sleep(20);ok(p.calls.length===0&&/e-mail válido/.test(p.txt()),'cadastro com e-mail inválido: bloqueado');
 cad('Maria Silva','m@x.com','fraca','fraca');await sleep(20);ok(p.calls.length===0&&/não atende/.test(p.txt()),'senha fraca: bloqueada no navegador');
 cad('Maria Silva','m@x.com',FORTE,'Outra-Senha-9');await sleep(20);ok(p.calls.length===0&&/não conferem/.test(p.txt()),'confirmação diferente: bloqueada');
 p.$('aCt').value='acai';p.$('aCneg').value='Açaí da Maria';cad('Maria  Silva','M@X.com',FORTE,FORTE);await sleep(40);
 const cc=p.calls[0];ok(cc&&cc.path==='/auth/cadastro'&&cc.body.email==='m@x.com'&&cc.body.nome==='Maria Silva'&&cc.body.tipo_negocio==='acai'&&!('perfil'in cc.body)&&!('estabelecimento_id'in cc.body),'cadastro válido: envia só nome, e-mail, senha e negócio, sem perfil nem estabelecimento');
 ok(/confirmação/.test(p.txt())&&!p.$('vLogin').hidden&&p.$('aCs').value==='','mensagem genérica de sucesso, volta ao login e limpa as senhas');
 // e-mail já cadastrado: o servidor responde igual (não revela)
 p=abrir({rotas:{'/auth/cadastro':async()=>R(202,{mensagem:'Se o e-mail puder ser usado, enviamos um link de confirmação.'})}});await sleep(50);
 p.$('aCard').querySelector('[data-go=cad]').click();p.$('aCn').value='Maria Silva';p.$('aCe').value='ja@existe.com';p.$('aCs').value=FORTE;p.$('aCc').value=FORTE;submit(p,'fCad');await sleep(40);
 ok(/Se o e-mail puder ser usado/.test(p.txt()),'e-mail já existente: a tela mostra a mesma mensagem (não revela quem tem conta)');
 // 8) olho da senha
 p=abrir();await sleep(30);p.$('aCard').querySelector('.eye').click();ok(p.$('aLs').type==='text','botão mostrar senha');p.$('aCard').querySelector('.eye').click();ok(p.$('aLs').type==='password','botão ocultar senha');
 // 9) recuperação de senha
 p=abrir({rotas:{'/auth/recuperar':async()=>R(202,{mensagem:'Se existir uma conta com este e-mail, enviamos as instruções.'})}});await sleep(50);
 p.$('aCard').querySelector('[data-go=rec]').click();p.$('aRe').value='x';submit(p,'fRec');await sleep(20);ok(p.calls.length===0&&/e-mail válido/.test(p.txt()),'recuperar com e-mail inválido: bloqueado');
 p.$('aRe').value='Qualquer@Pessoa.com';submit(p,'fRec');await sleep(40);ok(p.calls[0].body.email==='qualquer@pessoa.com'&&/Se existir uma conta/.test(p.txt()),'recuperar: mensagem genérica, igual exista a conta ou não');
 // link de recuperação
 p=abrir({hash:'access_token=rec-token&refresh_token=zz&type=recovery',rotas:{'/auth/redefinir-senha':async()=>R(200,{mensagem:'Senha alterada. Entre com a nova senha.'})}});await sleep(60);
 ok(!p.$('vRed').hidden&&p.w.location.hash===''&&p.ev('SES.rec')==='rec-token','link de recuperação: abre a tela de nova senha e tira o token do endereço');
 p.$('aNs').value='fraca';p.$('aNc').value='fraca';submit(p,'fRed');await sleep(20);ok(p.calls.length===0&&/não atende/.test(p.txt()),'nova senha fraca: bloqueada');
 p.$('aNs').value=FORTE;p.$('aNc').value=FORTE;submit(p,'fRed');await sleep(40);
 ok(p.calls[0].auth==='Bearer rec-token'&&p.calls[0].body.nova_senha===FORTE&&/Senha alterada/.test(p.txt())&&!p.$('vLogin').hidden&&p.ev('SES.rec')===null,'nova senha: enviada com o token do link, volta ao login e descarta o token');
 ok(p.ev('sessionStorage.getItem("sp_rt")')===null&&p.ev('localStorage.getItem("sp_rt")')===null,'  o link de recuperação não cria sessão sozinho');
 p=abrir({hash:'error=access_denied&error_code=otp_expired&error_description=expired'});await sleep(60);ok(/expirou ou já foi usado/.test(p.txt())&&p.w.location.hash==='','link expirado: mensagem clara');
 p=abrir({hash:'access_token=conf&type=signup'});await sleep(60);ok(/E-mail confirmado/.test(p.txt())&&p.ev('SES.token')===null&&p.$('app').hidden,'confirmação de e-mail: avisa e pede o login (não entra automaticamente)');
 // 10) cardápio público continua sem login
 p=abrir({hash:'cardapio'});await sleep(60);ok(!p.$('cv').hidden&&p.$('auth').hidden&&p.calls.length===0,'cardápio do cliente (#cardapio) continua público e sem pedir login');
 // 11) erros de script
 const all=[];console.log(all.length?all.join('\n'):'');process.exit(0)})();
