# Testes da tela de autenticação (navegador simulado)

Rodam a página com um navegador simulado (jsdom) e um servidor falso: não precisam de internet nem de banco.

    npm install jsdom
    set STOCKPULSE_HTML=C:\caminho\para\stockpulse.html     (PowerShell: $env:STOCKPULSE_HTML="...")
    node t7.js     # 49 cenários: login, cadastro, recuperação, logout, sessão, perfis, bloqueio sem login
    node t8.js     # regressão: todas as telas continuam abrindo e funcionando depois do login real
