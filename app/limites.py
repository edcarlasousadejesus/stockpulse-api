"""Limite de tentativas em memória (janela deslizante). Vale por processo: no Render, com 1 instância, protege de verdade;
com várias, cada uma conta separado, então mantenha também a proteção do Supabase e de um proxy/WAF."""
import time

from fastapi import HTTPException


class Janela:
    def __init__(self, maximo: int, segundos: int, msg: str = "muitas tentativas; aguarde alguns minutos e tente de novo"):
        self.maximo, self.segundos, self.msg, self.d = maximo, segundos, msg, {}

    def _recentes(self, k) -> list[float]:
        agora = time.time()
        if len(self.d) > 20000:
            self.d.clear()
        r = [t for t in self.d.get(k, []) if agora - t < self.segundos]
        self.d[k] = r
        return r

    def contar(self, k) -> int:
        return len(self._recentes(k))

    def registrar(self, k) -> None:
        self._recentes(k).append(time.time())

    def checar(self, k) -> None:
        r = self._recentes(k)
        if len(r) >= self.maximo:
            espera = max(1, int(self.segundos - (time.time() - r[0])))
            raise HTTPException(429, self.msg, headers={"Retry-After": str(espera)})

    def zerar(self, k) -> None:
        self.d.pop(k, None)

    def limpar(self) -> None:
        self.d.clear()
