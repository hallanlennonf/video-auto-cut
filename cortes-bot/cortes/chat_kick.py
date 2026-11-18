"""Chat do Kick pela API pública OFICIAL (webhooks).

Fluxo:
  1. Crie um app em https://kick.com/settings/developer, habilite webhooks e aponte a
     "Webhook URL" para um endereço público que chegue nesta máquina
     (ex.: um túnel do Cloudflare/ngrok para http://localhost:8787/kick).
  2. Exporte KICK_CLIENT_ID e KICK_CLIENT_SECRET.
  3. `python -m cortes kick-assinar`   -> inscreve os canais Kick do config no evento chat.message.sent
  4. `python -m cortes kick-webhook`   -> recebe os eventos e grava em dados/<canal>/chat/*.jsonl

AINDA NÃO TESTADO contra a API real: depende das credenciais do seu app.
Os nomes de endpoint/campos seguem a documentação pública do Kick (docs.kick.com);
confira lá se algo mudou.
"""
import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

TOKEN_URL = "https://id.kick.com/oauth/token"
API = "https://api.kick.com/public/v1"


def _token_app() -> str:
    cid, secret = os.environ.get("KICK_CLIENT_ID"), os.environ.get("KICK_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit("Defina KICK_CLIENT_ID e KICK_CLIENT_SECRET (app em kick.com/settings/developer).")
    r = httpx.post(TOKEN_URL, data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret}, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


def assinar_chat(slugs: list[str]) -> None:
    token = _token_app()
    h = {"Authorization": f"Bearer {token}"}
    for slug in slugs:
        r = httpx.get(f"{API}/channels", params={"slug": slug}, headers=h, timeout=20)
        r.raise_for_status()
        dados = r.json().get("data") or []
        if not dados:
            print(f"[kick:{slug}] canal não encontrado")
            continue
        bid = dados[0]["broadcaster_user_id"]
        r = httpx.post(
            f"{API}/events/subscriptions",
            headers=h,
            json={"broadcaster_user_id": bid, "events": [{"name": "chat.message.sent", "version": 1}], "method": "webhook"},
            timeout=20,
        )
        print(f"[kick:{slug}] assinatura -> HTTP {r.status_code}: {r.text[:200]}")


def _ts_evento(corpo: dict, headers) -> float:
    for valor in (corpo.get("created_at"), headers.get("Kick-Event-Message-Timestamp")):
        if valor:
            try:
                return datetime.fromisoformat(str(valor).replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    return time.time()


def servir_webhook(pasta_dados: Path, canais_kick: dict[str, str], porta: int) -> None:
    """canais_kick: {slug_no_kick: nome_do_canal_no_config}"""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            tamanho = int(self.headers.get("Content-Length", 0))
            corpo = json.loads(self.rfile.read(tamanho) or b"{}")
            self.send_response(200)
            self.end_headers()
            if self.headers.get("Kick-Event-Type") != "chat.message.sent":
                return
            slug = (corpo.get("broadcaster") or {}).get("channel_slug", "")
            nome = canais_kick.get(slug)
            if not nome:
                return
            ts = _ts_evento(corpo, self.headers)
            msg = {"ts": ts, "user": (corpo.get("sender") or {}).get("username", ""), "text": corpo.get("content", "")}
            dia = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            pasta = pasta_dados / nome / "chat"
            pasta.mkdir(parents=True, exist_ok=True)
            with open(pasta / f"{dia}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        def log_message(self, *_):
            pass

    print(f"[kick] webhook ouvindo em http://localhost:{porta}/kick")
    ThreadingHTTPServer(("127.0.0.1", porta), Handler).serve_forever()
