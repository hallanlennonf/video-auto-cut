"""Grava o chat de um canal da Twitch ao vivo, via IRC oficial em modo anônimo (somente leitura).

Cada mensagem vira uma linha JSON: {"ts": epoch_segundos, "user": ..., "text": ...}
Os arquivos são separados por dia (UTC) em dados/<canal>/chat/AAAA-MM-DD.jsonl.
Deixe rodando: quando o streamer não está ao vivo, simplesmente não chega mensagem.
"""
import asyncio
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets

IRC_URL = "wss://irc-ws.chat.twitch.tv:443"


def _parse_tags(raw: str) -> dict:
    tags = {}
    for par in raw.lstrip("@").split(";"):
        k, _, v = par.partition("=")
        tags[k] = v
    return tags


def parse_privmsg(linha: str) -> dict | None:
    """Converte uma linha IRC PRIVMSG (com tags) em dict, ou None se não for mensagem de chat."""
    if " PRIVMSG #" not in linha:
        return None
    tags = {}
    if linha.startswith("@"):
        raw_tags, _, linha = linha.partition(" ")
        tags = _parse_tags(raw_tags)
    prefixo, _, resto = linha.partition(" PRIVMSG #")
    _, _, texto = resto.partition(" :")
    user = tags.get("display-name") or prefixo.lstrip(":").split("!")[0]
    ts_ms = tags.get("tmi-sent-ts")
    ts = int(ts_ms) / 1000 if ts_ms and ts_ms.isdigit() else time.time()
    return {"ts": ts, "user": user, "text": texto}


def _arquivo_do_dia(pasta: Path, ts: float) -> Path:
    dia = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    return pasta / f"{dia}.jsonl"


async def gravar(login: str, pasta: Path) -> None:
    pasta.mkdir(parents=True, exist_ok=True)
    espera = 2
    while True:
        try:
            async with websockets.connect(IRC_URL, ping_interval=60) as ws:
                await ws.send("CAP REQ :twitch.tv/tags")
                await ws.send("PASS SCHMOOPIIE")
                await ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
                await ws.send(f"JOIN #{login.lower()}")
                print(f"[twitch:{login}] conectado, gravando chat em {pasta}")
                espera = 2
                async for bloco in ws:
                    for linha in bloco.split("\r\n"):
                        if not linha:
                            continue
                        if linha.startswith("PING"):
                            await ws.send(linha.replace("PING", "PONG", 1))
                            continue
                        msg = parse_privmsg(linha)
                        if msg:
                            with open(_arquivo_do_dia(pasta, msg["ts"]), "a", encoding="utf-8") as f:
                                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except (OSError, websockets.WebSocketException) as e:
            print(f"[twitch:{login}] conexão caiu ({e}); reconectando em {espera}s")
            await asyncio.sleep(espera)
            espera = min(espera * 2, 120)
