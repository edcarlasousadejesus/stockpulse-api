"""Política de senha forte. O Supabase guarda a senha com hash (bcrypt); aqui só validamos a força antes de enviar."""
import re
import unicodedata

COMUNS = {"123456", "12345678", "123456789", "1234567890", "password", "password1", "senha123", "senha1234", "qwerty123", "abc12345", "abcd1234",
          "admin123", "admin1234", "brasil123", "mudar123", "stockpulse", "stockpulse1", "iloveyou1", "senhasenha", "12345678a", "a1234567", "1q2w3e4r"}


def _norm(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", t.lower()) if not unicodedata.combining(c))


def problemas(senha: str, email: str = "", nome: str = "") -> list[str]:
    p = []
    if len(senha) < 10:
        p.append("use ao menos 10 caracteres")
    if len(senha.encode()) > 72:
        p.append("use no máximo 72 caracteres")
    if not re.search(r"[a-z]", senha):
        p.append("inclua uma letra minúscula")
    if not re.search(r"[A-Z]", senha):
        p.append("inclua uma letra maiúscula")
    if not re.search(r"\d", senha):
        p.append("inclua um número")
    s = _norm(senha)
    if s in COMUNS or re.fullmatch(r"(.)\1+", s) or s in ("1234567890", "0123456789", "abcdefghij"):
        p.append("esta senha é muito comum")
    local = _norm(email.split("@")[0])
    if len(local) >= 4 and local in s:
        p.append("não use o seu e-mail na senha")
    if any(len(t) >= 4 and t in s for t in _norm(nome).split()):
        p.append("não use o seu nome na senha")
    return p
