const {JSDOM}=require('jsdom');const fs=require('fs');
const HTML=fs.readFileSync(process.env.STOCKPULSE_HTML||'../stockpulse.html','utf8').replace(/<link[^>]+googleapis[^>]+>/,'').replace('<meta name="api-url" content="">','<meta name="api-url" content="https://api.test">');
const N=s=>s.replace(/[\u00a0\u202f]/g,' ');const ok=(c,m)=>console.log((c?'OK   ':'FALHA ')+m);const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const errs=[];
const dom=new JSDOM(HTML,{runScripts:'dangerously',pretendToBeVisual:true,url:'https://site.test/',beforeParse(w){
 w.HTMLCanvasElement.prototype.getContext=()=>new Proxy({},{get:()=>()=>({addColorStop(){}}),set:()=>true});w.HTMLCanvasElement.prototype.toDataURL=()=>'data:image/png;base64,AAAA';
 w.matchMedia=()=>({matches:true,addEventListener(){}});w.scrollTo=()=>{};w.open=()=>{};w.addEventListener('error',e=>errs.push(e.message));
 w.AudioContext=function(){this.currentTime=0;this.destination={};this.resume=()=>{};this.createOscillator=()=>({frequency:{},connect(){},start(){},stop(){}});this.createGain=()=>({gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){}})};
 w.fetch=async()=>({ok:true,status:200,headers:{get:()=>null},json:async()=>({access_token:'a',refresh_token:'r',expires_in:3600,usuario:{nome:'Ana',perfil:'administrador',estabelecimento_id:'e'}})})}});
const w=dom.window,d=w.document,$=id=>d.getElementById(id),ev=s=>w.eval(s),view=()=>N($('view').textContent);
const sub=()=>$('f').dispatchEvent(new w.Event('submit',{cancelable:true}));
(async()=>{await sleep(60);
 $('aLe').value='ana@empresa.com';$('aLs').value='x';$('fLogin').dispatchEvent(new w.Event('submit',{cancelable:true}));await sleep(60);
 ok(!$('app').hidden,'entrou pelo login real (servidor simulado)');
 for(const m of ['dash','ped','car','promo','cli','fech','prod','est','comp','ven','per','fic','rel','cfg']){try{ev(`go('${m}')`);ok(view().length>30,'módulo '+m+' abre')}catch(e){ok(false,m+': '+e.message)}}
 ev("go('dash')");ok(view().includes('Faturamento vs CMV')&&view().includes('Operação de hoje'),'painel com gráficos e operação de hoje');
 ev("go('ped')");$('sim').click();ok(!$('al').hidden,'alerta de pedido novo continua funcionando');ev(`aceita(${ev('pend()[0].id')})`);
 ev("go('ven')");const v0=ev('S.vendas.length');$('s1').value='Hambúrguer da casa';$('s2').value='5';sub();ok(ev('S.vendas.length')===v0+1&&view().includes('R$ 140,00'),'venda diária: 5 × R$ 28,00 = R$ 140,00');
 ev("go('cfg')");ok(view().includes('Tipo de negócio')&&view().includes('Marca do cardápio digital'),'ajustes: tipo de negócio e marca');
 ev("abrirCliente()");ok(!/stockpulse/i.test($('cv').innerHTML),'cardápio do cliente sem marca StockPulse');ev("fecharCliente()");
 ev("go('promo')");$('s1').value='0';$('s2').value='pct';$('s3').value='20';$('f').onsubmit(new w.Event('submit'));await sleep(80);ok(N($('pinfo').textContent).includes('R$ 22,40'),'promoções continuam calculando margem');
 console.log(errs.length?'ERROS: '+errs.join('\n'):'sem erros de script');process.exit(0)})();
