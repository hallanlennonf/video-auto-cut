"""TikTok: vínculo de conta (Login Kit para desktop, OAuth com PKCE) e envio pela Content Posting API.

Dois modos de envio, escolhidos ao aprovar cada corte:

- rascunho: o vídeo vai para a caixa de entrada do app do TikTok (escopo video.upload). Você abre a
  notificação no app, cola a legenda, escolhe som/capa e publica. Funciona em público mesmo com o app
  de desenvolvedor sem auditoria. Limite do TikTok: 5 rascunhos pendentes a cada 24h por conta.
- direto: publica no perfil já com legenda e privacidade (escopo video.publish). Enquanto o app não passar
  pela auditoria do TikTok, só funciona com a conta PRIVADA e o vídeo sai como "Só eu".

A senha nunca passa por aqui: o login acontece no site do TikTok e o painel recebe só os tokens, que
ficam em dados/_publicacao/tokens/ (access token vale 2h e é renovado sozinho; o refresh vale 180 dias).
"""
import hashlib
import json
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from . import config, contas
from .fila import ErroConta, ErroCota, ErroDefinitivo

AUTORIZAR = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
ESCOPOS = "user.info.basic,video.upload,video.publish"
REDIRECIONAMENTO = "http://localhost:{porta}/oauth/tiktok/callback"
PRIVACIDADES = {"PUBLIC_TO_EVERYONE": "Todo mundo", "MUTUAL_FOLLOW_FRIENDS": "Amigos",
                "FOLLOWER_OF_CREATOR": "Seguidores", "SELF_ONLY": "Só eu"}
MODOS = ("rascunho", "direto")
LIMITE_LEGENDA = 2200          # em unidades UTF-16, como o TikTok conta
MB = 1024 * 1024
INTERVALO_STATUS = 5           # segundos entre consultas do status (limite: 30/min)
ESPERA_STATUS = 300            # depois disso devolve "processando" e não trava a fila

_fluxos: dict[str, dict] = {}  # state do OAuth -> {verifier, redirect, criado}
_trava_token = threading.Lock()


class ErroTemporario(RuntimeError):
    """Limite por minuto, erro 5xx ou link de envio expirado: a fila tenta de novo em 15 min."""


def cliente() -> httpx.Client:  # substituído nos testes
    return httpx.Client(timeout=httpx.Timeout(30, write=300))


# ------------------------------------------------------------------ credencial do app

def _cfg(cfg: dict) -> dict:
    return cfg.get("publicacao", {}).get("tiktok", {})


def arquivo_credencial(cfg: dict) -> Path:
    return config.absoluto(_cfg(cfg).get("credencial", "segredos/tiktok_app.json"))


def _app(cfg: dict) -> dict:
    a = arquivo_credencial(cfg)
    if not a.exists():
        raise ValueError(f"falta a credencial do app do TikTok em {config.relativo(a)}")
    try:
        d = json.loads(a.read_text(encoding="utf-8-sig"))  # utf-8-sig: aceita arquivo salvo pelo Bloco de Notas
    except ValueError as e:
        raise ValueError(f"{config.relativo(a)} não é um JSON válido ({e})") from e
    if not d.get("client_key") or not d.get("client_secret"):
        raise ValueError(f"{config.relativo(a)} precisa ter client_key e client_secret")
    return d


def configurado(cfg: dict) -> bool:
    try:
        _app(cfg)
        return True
    except ValueError:
        return False


def modo_padrao(cfg: dict) -> str:
    m = _cfg(cfg).get("modo", "rascunho")
    return m if m in MODOS else "rascunho"


# ------------------------------------------------------------------ chamadas à API

def _erro(codigo: str, msg: str, status: int):
    explicacao = {
        "unaudited_client_can_only_post_to_private_accounts":
            "o app do TikTok ainda não passou pela auditoria: para postar direto, deixe a conta privada "
            "(ou use o modo rascunho)",
        "spam_risk_user_banned_from_posting": "o TikTok bloqueou esta conta de publicar por enquanto",
        "privacy_level_option_mismatch": "a privacidade escolhida não está disponível para esta conta",
        "spam_risk_too_many_posts": "limite diário de posts da conta atingido",
        "spam_risk_too_many_pending_share": "já há 5 rascunhos pendentes no app nas últimas 24h",
        "reached_active_user_cap": "o app atingiu o limite diário de contas publicando",
        "access_token_invalid": "acesso expirado ou revogado",
        "scope_not_authorized": "a conta não autorizou o envio de vídeos",
    }
    texto = f"{codigo}: {explicacao.get(codigo) or msg or 'erro do TikTok'}"
    if codigo in ("access_token_invalid", "scope_not_authorized") or status == 401:
        raise ErroConta(texto)
    if codigo in ("spam_risk_too_many_posts", "spam_risk_too_many_pending_share", "reached_active_user_cap"):
        raise ErroCota(texto)
    if codigo == "rate_limit_exceeded" or status == 429 or status >= 500:
        raise ErroTemporario(texto)
    raise ErroDefinitivo(texto)


def _chamar(caminho: str, acesso: str, corpo: dict | None = None, params: dict | None = None) -> dict:
    cab = {"Authorization": f"Bearer {acesso}"}
    try:
        with cliente() as c:
            if corpo is None:
                r = c.get(API + caminho, params=params, headers=cab)
            else:
                r = c.post(API + caminho, params=params, headers={**cab, "Content-Type": "application/json; charset=UTF-8"},
                           content=json.dumps(corpo))
    except httpx.TransportError as e:
        raise ErroTemporario(f"sem conexão com o TikTok ({e})") from e
    try:
        d = r.json()
    except ValueError:
        d = {}
    erro = d.get("error") or {}
    codigo = erro.get("code") or ("ok" if r.is_success else f"http_{r.status_code}")
    if codigo == "ok" and r.is_success:
        return d.get("data") or {}
    _erro(codigo, erro.get("message", "") or r.text[:200], r.status_code)


def _token(campos: dict) -> dict:
    try:
        with cliente() as c:
            r = c.post(API + "/oauth/token/", data=campos,
                       headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"})
    except httpx.TransportError as e:
        raise ErroTemporario(f"sem conexão com o TikTok ({e})") from e
    if r.status_code >= 500:
        raise ErroTemporario(f"TikTok indisponível (HTTP {r.status_code})")
    try:
        d = r.json()
    except ValueError:
        d = {}
    if not d.get("access_token"):
        raise ValueError(f"{d.get('error', f'http_{r.status_code}')}: {d.get('error_description', 'o TikTok recusou o token')}")
    agora = time.time()
    return {"access_token": d["access_token"], "refresh_token": d["refresh_token"],
            "expira_em": agora + int(d.get("expires_in", 0)),
            "refresh_expira_em": agora + int(d.get("refresh_expires_in", 0)),
            "open_id": d.get("open_id", ""), "scope": d.get("scope", "")}


def _acesso(conta_id: str, cfg: dict) -> dict:
    """Token válido da conta, renovando se faltar menos de 5 min (o TikTok troca o refresh a cada renovação)."""
    with _trava_token:
        tk = contas.ler_token(conta_id)
        if not tk:
            raise ErroConta("token não encontrado")
        if tk.get("expira_em", 0) - 300 > time.time():
            return tk
        if tk.get("refresh_expira_em", 0) <= time.time():
            raise ErroConta("a autorização venceu (180 dias)")
        app = _app(cfg)
        try:
            novo = _token({"client_key": app["client_key"], "client_secret": app["client_secret"],
                           "grant_type": "refresh_token", "refresh_token": tk["refresh_token"]})
        except ValueError as e:
            raise ErroConta(f"o TikTok recusou a renovação ({e})") from e
        tk = {**tk, **novo}
        contas.salvar_token(conta_id, tk)
        return tk


# ------------------------------------------------------------------ vínculo da conta

def iniciar_vinculo(cfg: dict, porta: int) -> str:
    """Devolve a URL do TikTok para o usuário autorizar a conta."""
    app = _app(cfg)
    agora = time.time()
    for s in [s for s, f in _fluxos.items() if agora - f["criado"] > 1800]:
        _fluxos.pop(s, None)
    verificador = secrets.token_urlsafe(64)               # 86 caracteres de A-Z a-z 0-9 - _
    desafio = hashlib.sha256(verificador.encode()).hexdigest()   # o TikTok desktop usa SHA256 em hex
    state = secrets.token_urlsafe(24)
    redir = REDIRECIONAMENTO.format(porta=porta)
    _fluxos[state] = {"verificador": verificador, "redirect": redir, "criado": agora}
    return AUTORIZAR + "?" + urlencode({
        "client_key": app["client_key"], "scope": ESCOPOS, "response_type": "code", "redirect_uri": redir,
        "state": state, "code_challenge": desafio, "code_challenge_method": "S256",
    })


def criador(acesso: str) -> dict:
    """Informações atuais do perfil para postar (nome, privacidades permitidas, interações, duração máxima)."""
    return _chamar("/post/publish/creator_info/query/", acesso, {})


def concluir_vinculo(state: str, code: str, cfg: dict) -> dict:
    fluxo = _fluxos.pop(state, None)
    if fluxo is None:
        raise ValueError("vínculo expirado ou inválido; clique em 'Vincular TikTok' de novo")
    app = _app(cfg)
    tk = _token({"client_key": app["client_key"], "client_secret": app["client_secret"], "code": code,
                 "grant_type": "authorization_code", "redirect_uri": fluxo["redirect"],
                 "code_verifier": fluxo["verificador"]})
    escopos = {s.strip() for s in tk["scope"].split(",") if s.strip()}
    if not escopos & {"video.upload", "video.publish"}:
        raise ValueError("a conta não autorizou o envio de vídeos; vincule de novo e deixe as permissões marcadas")
    usuario = nome = ""
    if "video.publish" in escopos:
        try:
            info = criador(tk["access_token"])
            usuario, nome = info.get("creator_username", ""), info.get("creator_nickname", "")
        except (ErroCota, ErroDefinitivo, ErroTemporario):
            pass  # ex.: limite de posts do dia; o nome vem do perfil básico
    if not nome:
        perfil = _chamar("/user/info/", tk["access_token"], params={"fields": "open_id,display_name"}).get("user", {})
        nome = perfil.get("display_name", "")
    modos = [m for m, e in (("rascunho", "video.upload"), ("direto", "video.publish")) if e in escopos]
    return contas.adicionar("tiktok", nome or usuario or "TikTok", tk["open_id"],
                            f"https://www.tiktok.com/@{usuario}" if usuario else "", tk,
                            extra={"usuario": usuario, "modos": modos})


def info_criador(conta: dict, cfg: dict) -> dict:
    """Para a janela de publicar: busca as opções atuais do perfil (o TikTok pede isso a cada envio)."""
    info = criador(_acesso(conta["id"], cfg)["access_token"])
    return {"nome": info.get("creator_nickname", "") or conta["nome"], "usuario": info.get("creator_username", ""),
            "privacidades": info.get("privacy_level_options") or [],
            "comentarios_off": bool(info.get("comment_disabled")), "dueto_off": bool(info.get("duet_disabled")),
            "costura_off": bool(info.get("stitch_disabled")),
            "duracao_max": info.get("max_video_post_duration_sec")}


# ------------------------------------------------------------------ envio

def _utf16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def montar_legenda(item: dict) -> str:
    """Legenda do TikTok: título, descrição, créditos e hashtags (o TikTok reconhece #tags na legenda)."""
    tags = " ".join("#" + str(h).strip().lstrip("#").replace(" ", "") for h in item.get("hashtags", []) if str(h).strip())
    credito = f"Créditos: {item['credito']}" if item.get("credito") else ""
    titulo, descricao = (item.get("titulo") or "").strip(), (item.get("descricao") or "").strip()
    fixo = "\n\n".join(p for p in (credito, tags) if p)
    texto = "\n\n".join(p for p in (titulo, descricao) if p)
    sobra = LIMITE_LEGENDA - _utf16(fixo) - (2 if fixo and texto else 0)
    if _utf16(texto) > sobra:  # corta o texto, nunca os créditos/hashtags
        while texto and _utf16(texto) > sobra - 1:
            texto = texto[:-1]
        texto = texto.rstrip() + "…"
    return "\n\n".join(p for p in (texto, fixo) if p)


def partes(tamanho: int) -> tuple[int, int]:
    """(tamanho do pedaço, quantidade): até 64 MB vai inteiro; acima, pedaços de 10 MB e o último absorve a sobra."""
    if tamanho <= 64 * MB:
        return tamanho, 1
    return 10 * MB, tamanho // (10 * MB)


def _enviar_arquivo(url: str, arquivo: Path, tamanho: int, pedaco: int, n: int) -> None:
    with cliente() as c, open(arquivo, "rb") as f:
        for k in range(n):
            ini = k * pedaco
            fim = tamanho - 1 if k == n - 1 else ini + pedaco - 1
            f.seek(ini)
            bloco = f.read(fim - ini + 1)
            cab = {"Content-Type": "video/mp4", "Content-Range": f"bytes {ini}-{fim}/{tamanho}"}
            for tentativa in range(3):
                try:
                    r = c.put(url, content=bloco, headers=cab)
                except httpx.TransportError as e:
                    if tentativa == 2:
                        raise ErroTemporario(f"envio do arquivo falhou ({e})") from e
                    time.sleep(3)
                    continue
                if r.status_code in (200, 201, 206):
                    break
                if r.status_code >= 500 and tentativa < 2:
                    time.sleep(3)
                    continue
                if r.status_code == 403 or r.status_code >= 500:
                    raise ErroTemporario(f"envio do arquivo falhou (HTTP {r.status_code})")
                raise ErroDefinitivo(f"o TikTok recusou o arquivo (HTTP {r.status_code})")


def _aguardar(publish_id: str, acesso: str) -> dict:
    limite = time.monotonic() + ESPERA_STATUS
    while True:
        st = _chamar("/post/publish/status/fetch/", acesso, {"publish_id": publish_id})
        estado = st.get("status", "")
        if estado == "FAILED":
            motivo = st.get("fail_reason", "")
            if motivo in ("spam_risk_too_many_posts",):
                raise ErroCota(f"{motivo}: limite diário de posts da conta atingido")
            if motivo == "auth_removed":
                raise ErroConta("a conta removeu a autorização do app")
            if motivo == "internal":
                raise ErroTemporario("erro interno do TikTok ao processar")
            raise ErroDefinitivo(f"o TikTok rejeitou o vídeo ({motivo or 'sem motivo'})")
        if estado in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX") or time.monotonic() >= limite:
            return st
        time.sleep(INTERVALO_STATUS)


def publicar(item: dict, conta: dict, cfg: dict) -> dict:
    opc = item.get("opcoes") or {}
    modo = opc.get("modo") or modo_padrao(cfg)
    if modo not in conta.get("modos", MODOS):
        raise ErroConta(f"a conta não autorizou o modo {modo}; vincule de novo")
    acesso = _acesso(conta["id"], cfg)["access_token"]
    arquivo = Path(item["arquivo"])
    tamanho = arquivo.stat().st_size
    pedaco, n = partes(tamanho)
    fonte = {"source": "FILE_UPLOAD", "video_size": tamanho, "chunk_size": pedaco, "total_chunk_count": n}
    legenda = montar_legenda(item)
    usuario = conta.get("usuario", "")

    if modo == "direto":
        info = criador(acesso)  # confere as opções de novo na hora de postar
        usuario = info.get("creator_username") or usuario
        maximo = info.get("max_video_post_duration_sec")
        if maximo and item.get("duracao") and item["duracao"] > maximo:
            raise ErroDefinitivo(f"a conta só aceita vídeos de até {maximo}s")
        priv = opc.get("privacidade")
        if priv not in (info.get("privacy_level_options") or []):
            raise ErroDefinitivo(f"privacidade '{PRIVACIDADES.get(priv, priv)}' indisponível para esta conta agora "
                                 f"(opções: {', '.join(PRIVACIDADES.get(p, p) for p in info.get('privacy_level_options') or [])})")
        post = {
            "title": legenda, "privacy_level": priv,
            "disable_comment": not opc.get("comentarios") or bool(info.get("comment_disabled")),
            "disable_duet": not opc.get("dueto") or bool(info.get("duet_disabled")),
            "disable_stitch": not opc.get("costura") or bool(info.get("stitch_disabled")),
            "brand_organic_toggle": bool(opc.get("marca_propria")),
            "brand_content_toggle": bool(opc.get("conteudo_marca")),
            "video_cover_timestamp_ms": 1000,
        }
        envio = _chamar("/post/publish/video/init/", acesso, {"post_info": post, "source_info": fonte})
    else:
        envio = _chamar("/post/publish/inbox/video/init/", acesso, {"source_info": fonte})

    _enviar_arquivo(envio["upload_url"], arquivo, tamanho, pedaco, n)
    st = _aguardar(envio["publish_id"], acesso)
    perfil = f"https://www.tiktok.com/@{usuario}" if usuario else (conta.get("url") or None)

    if modo == "rascunho":
        return {"publish_id": envio["publish_id"], "modo": "rascunho", "url": None, "legenda": legenda,
                "observacao": "Na caixa de entrada do app do TikTok: abra a notificação, cole a legenda e publique."}
    ids = st.get("publicaly_available_post_id") or []  # (sic) grafia da API; só vem para posts públicos
    concluido = st.get("status") == "PUBLISH_COMPLETE"
    obs = "" if concluido else "O TikTok ainda está processando; pode levar alguns minutos para aparecer no perfil."
    if concluido and opc.get("privacidade") == "SELF_ONLY":
        obs = "Publicado como “Só eu”."
    return {"publish_id": envio["publish_id"], "modo": "direto",
            "url": f"https://www.tiktok.com/@{usuario}/video/{ids[0]}" if ids and usuario else perfil,
            "privacidade": PRIVACIDADES.get(opc.get("privacidade"), opc.get("privacidade")), "observacao": obs}
