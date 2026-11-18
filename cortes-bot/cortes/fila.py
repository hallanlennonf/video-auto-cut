"""Fila de publicação com agendamento.

Cada item é "publicar o corte X na conta Y no horário Z". O trabalhador (uma thread que roda junto
com o painel) confere a fila a cada 20s e publica o que já venceu. O PC precisa estar ligado com o
painel aberto no horário agendado; o que vencer com o PC desligado sai assim que o painel abrir.

Agendamento automático: próximo horário livre da conta (config publicacao.horarios), respeitando
max_por_dia_por_conta e, no YouTube, a cota diária do projeto (envios_por_dia, que zera à meia-noite
do horário do Pacífico, como a cota do Google).
"""
import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, contas

PACIFICO = ZoneInfo("America/Los_Angeles")
ATIVOS = ("agendado", "enviando", "publicado")
_trava = threading.RLock()


class ErroCota(Exception):
    """Cota diária da plataforma esgotada: reagendar para o dia seguinte."""


class ErroConta(Exception):
    """Acesso revogado/expirado: a conta precisa ser vinculada de novo."""


class ErroDefinitivo(Exception):
    """O envio não vai funcionar tentando de novo (ex.: arquivo sumiu, vídeo rejeitado)."""


def _arq() -> Path:
    return contas.pasta() / "fila.json"


def listar() -> list[dict]:
    with _trava:
        a = _arq()
        return json.loads(a.read_text(encoding="utf-8")) if a.exists() else []


def _salvar(itens: list[dict]) -> None:
    a = _arq()
    tmp = a.with_suffix(".tmp")
    tmp.write_text(json.dumps(itens, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(a)


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def proximo_horario(conta: dict, itens: list[dict], cfg: dict, depois: datetime | None = None) -> datetime:
    """Próximo horário de publicação livre para a conta (UTC)."""
    pub = cfg["publicacao"]
    fuso = ZoneInfo(pub["fuso"])
    limite = (depois or _agora()) + timedelta(minutes=2)
    horarios = sorted(datetime.strptime(h, "%H:%M").time() for h in pub["horarios"])
    da_conta = [i for i in itens if i["conta_id"] == conta["id"] and i["status"] in ATIVOS]
    youtube = [i for i in itens if i["plataforma"] == "youtube" and i["status"] in ATIVOS]
    dia = limite.astimezone(fuso).date()
    for _ in range(120):
        for h in horarios:
            t = datetime.combine(dia, h, tzinfo=fuso).astimezone(timezone.utc)
            if t < limite:
                continue
            mesmo_dia = [i for i in da_conta if _dt(i["agendado_para"]).astimezone(fuso).date() == dia]
            if len(mesmo_dia) >= pub["max_por_dia_por_conta"]:
                break
            if any(abs((_dt(i["agendado_para"]) - t).total_seconds()) < 60 for i in mesmo_dia):
                continue
            if conta["plataforma"] == "youtube":
                dia_pac = t.astimezone(PACIFICO).date()
                usados = sum(1 for i in youtube if _dt(i["agendado_para"]).astimezone(PACIFICO).date() == dia_pac)
                if usados >= pub["youtube"]["envios_por_dia"]:
                    continue
            return t
        dia += timedelta(days=1)
    raise RuntimeError("não há horário livre nos próximos 120 dias")


def _privacidade(plataforma: str, privacidade: str | None, opcoes: dict, cfg: dict) -> str | None:
    if plataforma == "youtube":
        return privacidade or cfg["publicacao"].get("youtube", {}).get("privacidade", "public")
    if plataforma == "tiktok":
        return opcoes.get("privacidade") if opcoes.get("modo") == "direto" else "rascunho"
    return None


def agendar(corte: dict, conta_ids: list[str], modo: str, cfg: dict, data_local: str | None = None,
            privacidade: str | None = None, opcoes: dict | None = None) -> list[dict]:
    """Cria um item na fila para cada conta. `corte` traz id, arquivo, titulo, descricao, hashtags, credito, duracao.
    modo: "proximo" (próximo horário livre), "agora" ou "data" (data_local = 'AAAA-MM-DDTHH:MM' no fuso do config).
    privacidade: a do YouTube. opcoes: por plataforma, ex. {"tiktok": {"modo": "direto", "privacidade": ...}}."""
    novos = []
    with _trava:
        itens = listar()
        for cid in conta_ids:
            conta = contas.obter(cid)
            if conta is None:
                raise ValueError(f"conta {cid} não existe")
            if any(i["corte_id"] == corte["id"] and i["conta_id"] == cid and i["status"] in ATIVOS for i in itens):
                continue  # já está na fila/publicado nessa conta
            if modo == "agora":
                quando = _agora()
            elif modo == "data":
                fuso = ZoneInfo(cfg["publicacao"]["fuso"])
                quando = datetime.fromisoformat(data_local).replace(tzinfo=fuso).astimezone(timezone.utc)
            else:
                quando = proximo_horario(conta, itens, cfg)
            op = (opcoes or {}).get(conta["plataforma"], {})
            item = {
                "id": uuid.uuid4().hex[:10], "conta_id": cid, "conta_nome": conta["nome"],
                "plataforma": conta["plataforma"], "corte_id": corte["id"], "arquivo": corte["arquivo"],
                "titulo": corte["titulo"], "descricao": corte.get("descricao", ""),
                "hashtags": corte.get("hashtags", []), "credito": corte.get("credito", ""),
                "duracao": corte.get("duracao"), "opcoes": op,
                "privacidade": _privacidade(conta["plataforma"], privacidade, op, cfg),
                "agendado_para": quando.isoformat(timespec="seconds"), "status": "agendado", "tentativas": 0,
                "erro": None, "resultado": None, "criado_em": _agora().isoformat(timespec="seconds"),
            }
            itens.append(item)
            novos.append(item)
        _salvar(itens)
    return novos


def acao(item_id: str, qual: str) -> dict:
    """cancelar | tentar (de novo, no próximo horário livre) | agora (publicar já)."""
    with _trava:
        itens = listar()
        item = next(i for i in itens if i["id"] == item_id)
        if qual == "cancelar" and item["status"] in ("agendado", "erro"):
            item["status"] = "cancelado"
        elif qual in ("tentar", "agora") and item["status"] in ("agendado", "erro", "cancelado"):
            conta = contas.obter(item["conta_id"])
            if conta is None:
                raise ValueError("a conta deste item foi removida")
            outros = [i for i in itens if i["id"] != item_id]
            quando = _agora() if qual == "agora" else proximo_horario(conta, outros, config.carregar())
            item.update(status="agendado", erro=None, agendado_para=quando.isoformat(timespec="seconds"))
        else:
            raise ValueError(f"não dá para '{qual}' um item com status '{item['status']}'")
        _salvar(itens)
        return item


def _atualizar(item_id: str, **campos) -> dict:
    with _trava:
        itens = listar()
        item = next(i for i in itens if i["id"] == item_id)
        item.update(campos, atualizado_em=_agora().isoformat(timespec="seconds"))
        _salvar(itens)
        return item


def _publicador(plataforma: str):
    if plataforma == "youtube":
        from . import pub_youtube
        return pub_youtube.publicar
    if plataforma == "tiktok":
        from . import pub_tiktok
        return pub_tiktok.publicar
    if plataforma == "simulacao":
        from . import pub_simulacao
        return pub_simulacao.publicar
    raise ErroDefinitivo(f"plataforma sem publicador: {plataforma}")


def executar_vencidos(log=print) -> int:
    """Publica, um por vez, os itens agendados que já venceram. Devolve quantos tentou."""
    cfg = config.carregar()
    feitos = 0
    while True:
        with _trava:
            vencidos = sorted((i for i in listar() if i["status"] == "agendado" and _dt(i["agendado_para"]) <= _agora()),
                              key=lambda i: i["agendado_para"])
            if not vencidos:
                return feitos
            item = _atualizar(vencidos[0]["id"], status="enviando", tentativas=vencidos[0]["tentativas"] + 1)
        feitos += 1
        conta = contas.obter(item["conta_id"])
        try:
            if conta is None:
                raise ErroDefinitivo("a conta foi removida")
            if conta.get("precisa_reconectar"):
                raise ErroConta("a conta precisa ser vinculada de novo")
            arquivo = config.absoluto(item["arquivo"])
            if not arquivo.exists():
                raise ErroDefinitivo(f"arquivo não encontrado: {item['arquivo']}")
            log(f"[fila] publicando '{item['titulo']}' em {conta['nome']} ({conta['plataforma']})...")
            resultado = _publicador(item["plataforma"])({**item, "arquivo": str(arquivo)}, conta, cfg)
            _atualizar(item["id"], status="publicado", resultado=resultado, erro=None)
            log(f"[fila] publicado: {resultado.get('url')}")
        except ErroCota as e:
            with _trava:
                outros = [i for i in listar() if i["id"] != item["id"]]
                amanha = datetime.now(PACIFICO).replace(hour=0, minute=5, second=0, microsecond=0) + timedelta(days=1)
                quando = proximo_horario(conta, outros, cfg, depois=amanha.astimezone(timezone.utc))
                _atualizar(item["id"], status="agendado", erro=f"cota esgotada: {e}; reagendado",
                           agendado_para=quando.isoformat(timespec="seconds"))
            log(f"[fila] cota esgotada; reagendado para {quando.isoformat()}")
        except ErroConta as e:
            contas.atualizar(conta["id"], precisa_reconectar=True)
            _atualizar(item["id"], status="erro", erro=f"reconecte a conta: {e}")
            log(f"[fila] conta {conta['nome']} precisa ser reconectada: {e}")
        except ErroDefinitivo as e:
            _atualizar(item["id"], status="erro", erro=str(e))
            log(f"[fila] erro: {e}")
        except Exception as e:  # erro temporário (rede, 5xx...): tenta de novo em 15 min, até 3 vezes
            if item["tentativas"] < 3:
                novo = (_agora() + timedelta(minutes=15)).isoformat(timespec="seconds")
                _atualizar(item["id"], status="agendado", erro=f"falhou ({e}); nova tentativa às {novo}", agendado_para=novo)
            else:
                _atualizar(item["id"], status="erro", erro=f"falhou 3 vezes: {e}")
            log(f"[fila] falha temporária: {e}\n{traceback.format_exc(limit=3)}")


def iniciar_trabalhador(intervalo: int = 20) -> threading.Thread:
    def laco():
        while True:
            try:
                executar_vencidos()
            except Exception:
                traceback.print_exc()
            time.sleep(intervalo)

    t = threading.Thread(target=laco, name="fila-publicacao", daemon=True)
    t.start()
    return t
