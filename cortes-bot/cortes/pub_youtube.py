"""YouTube: vínculo de canal (OAuth do Google) e envio de vídeos pela YouTube Data API v3.

O login acontece no site do Google; o painel só recebe um token de acesso, que fica salvo em
dados/_publicacao/tokens/. Cada vínculo corresponde a UM canal (se a conta Google tiver vários
canais, o Google pergunta qual na hora de autorizar).
"""
import json
import os
import re
from pathlib import Path

from . import config, contas
from .fila import ErroConta, ErroCota, ErroDefinitivo

ESCOPOS = [
    "https://www.googleapis.com/auth/youtube.upload",     # enviar vídeos
    "https://www.googleapis.com/auth/youtube.readonly",   # ler o nome do canal ao vincular
]
REDIRECIONAMENTO = "http://localhost:{porta}/oauth/youtube/callback"

_fluxos: dict = {}  # state do OAuth -> Flow em andamento


def arquivo_credencial(cfg: dict) -> Path:
    return config.RAIZ / cfg["publicacao"]["youtube"]["client_secret"]


def configurado(cfg: dict) -> bool:
    return arquivo_credencial(cfg).exists()


def iniciar_vinculo(cfg: dict, porta: int) -> str:
    """Devolve a URL do Google para o usuário autorizar o canal."""
    from google_auth_oauthlib.flow import Flow

    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    fluxo = Flow.from_client_secrets_file(str(arquivo_credencial(cfg)), scopes=ESCOPOS,
                                          redirect_uri=REDIRECIONAMENTO.format(porta=porta))
    url, state = fluxo.authorization_url(access_type="offline", prompt="consent select_account",
                                         include_granted_scopes="true")
    _fluxos[state] = fluxo
    return url


def concluir_vinculo(state: str, code: str) -> dict:
    from googleapiclient.discovery import build

    fluxo = _fluxos.pop(state, None)
    if fluxo is None:
        raise ValueError("vínculo expirado ou inválido; clique em 'Vincular YouTube' de novo")
    fluxo.fetch_token(code=code)
    cred = fluxo.credentials
    yt = build("youtube", "v3", credentials=cred, cache_discovery=False)
    itens = yt.channels().list(part="snippet", mine=True).execute().get("items") or []
    if not itens:
        raise ValueError("essa conta Google não tem canal do YouTube")
    canal = itens[0]
    return contas.adicionar("youtube", canal["snippet"]["title"], canal["id"],
                            f"https://www.youtube.com/channel/{canal['id']}", json.loads(cred.to_json()))


def _limpo(s: str) -> str:
    return re.sub(r"[<>]", "", s or "").strip()  # o YouTube rejeita < e > no título/descrição


def montar_corpo(item: dict, cfg: dict) -> dict:
    yt = cfg["publicacao"]["youtube"]
    tags = [_limpo(h).lstrip("#") for h in item.get("hashtags", []) if _limpo(h)]
    while tags and sum(len(t) + 1 for t in tags) > 450:  # limite de ~500 caracteres nas tags
        tags.pop()
    partes = [_limpo(item.get("descricao", ""))]
    if item.get("credito"):
        partes.append(f"Créditos: {item['credito']}")
    if tags:
        partes.append(" ".join(f"#{t.replace(' ', '')}" for t in tags))
    titulo = _limpo(item["titulo"])[:100]
    if not titulo:
        raise ErroDefinitivo("título vazio")
    return {
        "snippet": {"title": titulo, "description": "\n\n".join(p for p in partes if p)[:4900],
                    "tags": tags, "categoryId": str(yt.get("categoria", "20"))},
        "status": {"privacyStatus": item.get("privacidade") or yt.get("privacidade", "public"),
                   "selfDeclaredMadeForKids": False, "embeddable": True},
    }


def _credenciais(conta: dict):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token = contas.ler_token(conta["id"])
    if not token:
        raise ErroConta("token não encontrado")
    cred = Credentials.from_authorized_user_info(token, ESCOPOS)
    if not cred.valid:
        try:
            cred.refresh(Request())
        except RefreshError as e:
            raise ErroConta(f"o Google recusou o acesso ({e})") from e
        contas.salvar_token(conta["id"], json.loads(cred.to_json()))
    return cred


def _motivo(erro) -> tuple[str, str]:
    try:
        corpo = json.loads(erro.content.decode("utf-8"))["error"]
        return (corpo.get("errors") or [{}])[0].get("reason", ""), corpo.get("message", "")
    except (ValueError, KeyError, AttributeError):
        return "", str(erro)


def publicar(item: dict, conta: dict, cfg: dict) -> dict:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    corpo = montar_corpo(item, cfg)
    yt = build("youtube", "v3", credentials=_credenciais(conta), cache_discovery=False)
    midia = MediaFileUpload(item["arquivo"], mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    pedido = yt.videos().insert(part="snippet,status", body=corpo, media_body=midia)
    resposta = None
    try:
        while resposta is None:
            _, resposta = pedido.next_chunk(num_retries=3)
    except HttpError as e:
        motivo, msg = _motivo(e)
        status = e.resp.status
        if motivo in ("quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded", "rateLimitExceeded"):
            raise ErroCota(f"{motivo}: {msg}") from e
        if status == 401 or (motivo in ("authError", "forbidden") and "insufficient" in msg.lower()):
            raise ErroConta(f"{motivo}: {msg}") from e
        if 400 <= status < 500:
            raise ErroDefinitivo(f"YouTube recusou ({status} {motivo}): {msg}") from e
        raise  # 5xx: temporário, a fila tenta de novo
    vid = resposta["id"]
    return {"video_id": vid, "url": f"https://youtube.com/shorts/{vid}",
            "privacidade": resposta.get("status", {}).get("privacyStatus")}
