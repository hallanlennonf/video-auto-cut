"""Contas vinculadas para publicação (YouTube, e depois TikTok/Instagram).

- dados/_publicacao/contas.json   lista de contas (sem segredos): o painel pode mostrar
- dados/_publicacao/tokens/<id>.json  tokens de acesso de cada conta: NUNCA saem do seu PC
                                      nem são devolvidos pelo painel
"""
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config

PLATAFORMAS = {
    "youtube": {"nome": "YouTube", "duracao_max": 180},
    "simulacao": {"nome": "Simulação (não publica nada)", "duracao_max": 10_000},
}

_trava = threading.RLock()


def pasta() -> Path:
    p = config.RAIZ / config.carregar()["pasta_dados"] / "_publicacao"
    (p / "tokens").mkdir(parents=True, exist_ok=True)
    return p


def _arq() -> Path:
    return pasta() / "contas.json"


def listar() -> list[dict]:
    with _trava:
        a = _arq()
        return json.loads(a.read_text(encoding="utf-8")) if a.exists() else []


def _salvar(contas: list[dict]) -> None:
    a = _arq()
    tmp = a.with_suffix(".tmp")
    tmp.write_text(json.dumps(contas, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(a)


def obter(conta_id: str) -> dict | None:
    return next((c for c in listar() if c["id"] == conta_id), None)


def adicionar(plataforma: str, nome: str, externo_id: str = "", url: str = "", token: dict | None = None) -> dict:
    """Cria a conta, ou atualiza o token se o mesmo canal externo já estava vinculado."""
    if plataforma not in PLATAFORMAS:
        raise ValueError(f"plataforma desconhecida: {plataforma}")
    with _trava:
        contas = listar()
        conta = next((c for c in contas if externo_id and c["plataforma"] == plataforma and c["externo_id"] == externo_id), None)
        if conta is None:
            conta = {"id": f"{plataforma[:2]}-{uuid.uuid4().hex[:8]}", "plataforma": plataforma, "nome": nome,
                     "externo_id": externo_id, "url": url, "canais": [], "precisa_reconectar": False,
                     "vinculada_em": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            contas.append(conta)
        conta.update(nome=nome, url=url, precisa_reconectar=False)
        _salvar(contas)
        if token is not None:
            salvar_token(conta["id"], token)
        return conta


def atualizar(conta_id: str, canais: list[str] | None = None, precisa_reconectar: bool | None = None) -> dict:
    validos = set(config.carregar()["canais"])
    with _trava:
        contas = listar()
        conta = next(c for c in contas if c["id"] == conta_id)
        if canais is not None:
            conta["canais"] = [c for c in canais if c in validos]
        if precisa_reconectar is not None:
            conta["precisa_reconectar"] = precisa_reconectar
        _salvar(contas)
        return conta


def remover(conta_id: str) -> None:
    with _trava:
        _salvar([c for c in listar() if c["id"] != conta_id])
        t = pasta() / "tokens" / f"{conta_id}.json"
        if t.exists():
            t.unlink()


def ler_token(conta_id: str) -> dict | None:
    t = pasta() / "tokens" / f"{conta_id}.json"
    return json.loads(t.read_text(encoding="utf-8")) if t.exists() else None


def salvar_token(conta_id: str, token: dict) -> None:
    t = pasta() / "tokens" / f"{conta_id}.json"
    tmp = t.with_suffix(".tmp")
    tmp.write_text(json.dumps(token), encoding="utf-8")
    tmp.replace(t)


def compativel(conta: dict, duracao: float) -> bool:
    return duracao <= PLATAFORMAS.get(conta["plataforma"], {}).get("duracao_max", 0)
