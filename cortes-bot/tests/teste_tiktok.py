"""Teste da integração com o TikTok usando um TikTok falso (nada sai do seu PC, nada é publicado):
vínculo com PKCE, renovação do token, envio em pedaços, modos direto e rascunho, erros e o painel.

  python tests/teste_tiktok.py
"""
import copy
import hashlib
import http.client
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cortes import config, contas, fila, painel, pub_tiktok  # noqa: E402

ESCOPOS = "user.info.basic,video.upload,video.publish"


def resposta(status: int, corpo: dict) -> httpx.Response:
    return httpx.Response(status, json=corpo)


def ok(dados: dict) -> httpx.Response:
    return resposta(200, {"data": dados, "error": {"code": "ok", "message": "", "log_id": "x"}})


def erro(status: int, codigo: str) -> httpx.Response:
    return resposta(status, {"data": {}, "error": {"code": codigo, "message": "msg do tiktok", "log_id": "x"}})


class TikTokFalso:
    def __init__(self):
        self.privacidades = ["FOLLOWER_OF_CREATOR", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"]
        self.status = ["PROCESSING_UPLOAD", "PUBLISH_COMPLETE"]
        self.erro_init = None
        self.renovacoes = 0
        self.pedacos, self.inits, self.tokens = [], [], []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if req.url.host == "upload.falso":
            faixa = req.headers["content-range"]
            self.pedacos.append((faixa, len(req.content)))
            ini_fim, total = faixa.removeprefix("bytes ").split("/")
            return httpx.Response(201 if int(ini_fim.split("-")[1]) == int(total) - 1 else 206)
        if p == "/v2/oauth/token/":
            f = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            self.tokens.append(f)
            if f.get("code") == "ruim":
                return resposta(400, {"error": "invalid_grant", "error_description": "code expirado", "log_id": "x"})
            n = 1
            if f["grant_type"] == "refresh_token":
                self.renovacoes += 1
                n = self.renovacoes + 1
            return resposta(200, {"access_token": f"at{n}", "refresh_token": f"rt{n}", "expires_in": 7200,
                                  "refresh_expires_in": 15552000, "open_id": "oid-123", "scope": ESCOPOS, "token_type": "Bearer"})
        assert req.headers["authorization"].startswith("Bearer at"), req.headers
        if p == "/v2/post/publish/creator_info/query/":
            return ok({"creator_username": "cortesdoalan", "creator_nickname": "Cortes do Alan",
                       "privacy_level_options": self.privacidades, "comment_disabled": False,
                       "duet_disabled": True, "stitch_disabled": False, "max_video_post_duration_sec": 600})
        if p in ("/v2/post/publish/video/init/", "/v2/post/publish/inbox/video/init/"):
            self.inits.append((p, json.loads(req.content)))
            if self.erro_init:
                return erro(*self.erro_init)
            return ok({"publish_id": "v_pub_1", "upload_url": "https://upload.falso/video/?id=1"})
        if p == "/v2/post/publish/status/fetch/":
            s = self.status.pop(0) if len(self.status) > 1 else self.status[0]
            return ok({"status": s, "publicaly_available_post_id": [7300000000000000001] if s == "PUBLISH_COMPLETE" else []})
        if p == "/v2/user/info/":
            return ok({"user": {"open_id": "oid-123", "display_name": "Alan"}})
        return httpx.Response(404)


def esperar_erro(tipo, f, *args, contem=""):
    try:
        f(*args)
    except tipo as e:
        assert contem in str(e), (contem, str(e))
        return e
    raise AssertionError(f"esperava {tipo.__name__}")


def main():
    falso = TikTokFalso()
    pub_tiktok.cliente = lambda: httpx.Client(transport=httpx.MockTransport(falso))
    pub_tiktok.INTERVALO_STATUS = 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "tokens").mkdir()
        contas.pasta = lambda: tmp  # isola: não toca nas contas/fila reais
        cfg = config.carregar()
        cfg["publicacao"]["tiktok"] = {"credencial": str(tmp / "tiktok_app.json"), "modo": "rascunho"}
        config.carregar = lambda *a, **k: copy.deepcopy(cfg)

        # --- credencial
        assert not pub_tiktok.configurado(cfg)
        (tmp / "tiktok_app.json").write_text('﻿{"client_key": "ck_teste", "client_secret": "cs_teste"}', encoding="utf-8")
        assert pub_tiktok.configurado(cfg), "deve aceitar arquivo com BOM (Bloco de Notas)"
        print("ok  credencial do app lida de segredos/ (aceita BOM)")

        # --- vínculo: PKCE (SHA256 em hex, como o TikTok desktop pede) e troca do código
        url = urlparse(pub_tiktok.iniciar_vinculo(cfg, 8800))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        assert url.netloc == "www.tiktok.com" and q["client_key"] == "ck_teste" and q["scope"] == ESCOPOS
        assert q["redirect_uri"] == "http://localhost:8800/oauth/tiktok/callback" and q["code_challenge_method"] == "S256"
        verificador = pub_tiktok._fluxos[q["state"]]["verificador"]
        assert 43 <= len(verificador) <= 128 and q["code_challenge"] == hashlib.sha256(verificador.encode()).hexdigest()
        esperar_erro(ValueError, pub_tiktok.concluir_vinculo, "state-falso", "abc", cfg, contem="expirado")
        conta = pub_tiktok.concluir_vinculo(q["state"], "codigo-bom", cfg)
        assert falso.tokens[-1]["code_verifier"] == verificador and falso.tokens[-1]["redirect_uri"] == q["redirect_uri"]
        assert conta["plataforma"] == "tiktok" and conta["nome"] == "Cortes do Alan" and conta["usuario"] == "cortesdoalan"
        assert conta["url"] == "https://www.tiktok.com/@cortesdoalan" and conta["modos"] == ["rascunho", "direto"]
        assert "at1" not in (tmp / "contas.json").read_text(encoding="utf-8"), "token não pode ir para contas.json"
        assert contas.ler_token(conta["id"])["refresh_token"] == "rt1"
        print("ok  vínculo: PKCE em hex, troca do código, @usuario e token guardado à parte")

        # --- renovação do token (o TikTok troca o refresh token a cada renovação)
        tk = contas.ler_token(conta["id"])
        contas.salvar_token(conta["id"], {**tk, "expira_em": time.time() - 10})
        assert pub_tiktok._acesso(conta["id"], cfg)["access_token"] == "at2"
        assert falso.tokens[-1] == {"client_key": "ck_teste", "client_secret": "cs_teste",
                                    "grant_type": "refresh_token", "refresh_token": "rt1"}
        assert contas.ler_token(conta["id"])["refresh_token"] == "rt2"
        assert pub_tiktok._acesso(conta["id"], cfg)["access_token"] == "at2" and falso.renovacoes == 1, "não renova à toa"
        contas.salvar_token(conta["id"], {**contas.ler_token(conta["id"]), "expira_em": 0, "refresh_expira_em": 0})
        esperar_erro(fila.ErroConta, pub_tiktok._acesso, conta["id"], cfg, contem="180 dias")
        contas.salvar_token(conta["id"], {**contas.ler_token(conta["id"]), "expira_em": time.time() + 7200})
        print("ok  token renovado sozinho, refresh novo salvo; vencido marca para reconectar")

        # --- legenda
        item_base = {"titulo": "Alan bugou o GTA 6", "descricao": "Fuga da polícia 😂 " * 200,
                     "hashtags": ["#gta6", "alanzoka", "cortes do alan"], "credito": "twitch.tv/alanzoka"}
        leg = pub_tiktok.montar_legenda(item_base)
        assert pub_tiktok._utf16(leg) <= 2200 and leg.startswith("Alan bugou o GTA 6")
        assert leg.endswith("Créditos: twitch.tv/alanzoka\n\n#gta6 #alanzoka #cortesdoalan") and "…" in leg
        print("ok  legenda: título, descrição cortada em 2200 (UTF-16), créditos e hashtags preservados")

        # --- envio em pedaços (regra do TikTok: até 64 MB inteiro; acima, pedaços e o último absorve a sobra)
        assert pub_tiktok.partes(3 * 1024 * 1024) == (3 * 1024 * 1024, 1)
        assert pub_tiktok.partes(64 * 1024 * 1024) == (64 * 1024 * 1024, 1)
        assert pub_tiktok.partes(155 * 1024 * 1024 + 123) == (10 * 1024 * 1024, 15)
        video = tmp / "corte.mp4"
        video.write_bytes(b"\x01" * (155 * 1024 + 123))
        mb_original = pub_tiktok.MB
        pub_tiktok.MB = 1024  # encolhe a escala para testar vários pedaços com um arquivo pequeno
        try:
            item = {**item_base, "id": "q1", "arquivo": str(video), "duracao": 95,
                    "opcoes": {"modo": "direto", "privacidade": "FOLLOWER_OF_CREATOR", "comentarios": True, "dueto": True}}
            r = pub_tiktok.publicar(item, conta, cfg)
        finally:
            pub_tiktok.MB = mb_original
        caminho, corpo = falso.inits[-1]
        assert caminho == "/v2/post/publish/video/init/"
        assert corpo["source_info"] == {"source": "FILE_UPLOAD", "video_size": 155 * 1024 + 123,
                                        "chunk_size": 10 * 1024, "total_chunk_count": 15}, corpo["source_info"]
        post = corpo["post_info"]
        assert post["privacy_level"] == "FOLLOWER_OF_CREATOR" and post["title"] == pub_tiktok.montar_legenda(item)
        assert post["disable_comment"] is False, "comentários liberados pelo usuário"
        assert post["disable_duet"] is True, "dueto desativado no perfil vence a escolha"
        assert post["disable_stitch"] is True, "costura não marcada = desativada"
        assert post["brand_content_toggle"] is False and post["brand_organic_toggle"] is False
        assert len(falso.pedacos) == 15 and falso.pedacos[0] == ("bytes 0-10239/158843", 10240)
        assert falso.pedacos[-1] == (f"bytes {14 * 10240}-158842/158843", 158843 - 14 * 10240)
        assert r["url"] == "https://www.tiktok.com/@cortesdoalan/video/7300000000000000001" and r["modo"] == "direto"
        print("ok  postagem direta: 15 pedaços com Content-Range certo, opções do perfil respeitadas, link do vídeo")

        # privacidade que o perfil não oferece (ex.: virou conta privada depois do agendamento)
        esperar_erro(fila.ErroDefinitivo, pub_tiktok.publicar,
                     {**item, "opcoes": {"modo": "direto", "privacidade": "PUBLIC_TO_EVERYONE"}}, conta, cfg, contem="indisponível")
        esperar_erro(fila.ErroDefinitivo, pub_tiktok.publicar, {**item, "duracao": 900}, conta, cfg, contem="até 600s")
        print("ok  confere de novo o perfil na hora de postar (privacidade e duração)")

        # --- rascunho
        falso.status = ["PROCESSING_UPLOAD", "SEND_TO_USER_INBOX"]
        r = pub_tiktok.publicar({**item_base, "id": "q2", "arquivo": str(video), "opcoes": {"modo": "rascunho"}}, conta, cfg)
        caminho, corpo = falso.inits[-1]
        assert caminho == "/v2/post/publish/inbox/video/init/" and "post_info" not in corpo
        assert falso.pedacos[-1] == ("bytes 0-158842/158843", 158843), "arquivo pequeno vai num pedaço só"
        assert r["modo"] == "rascunho" and r["url"] is None and r["legenda"].startswith("Alan bugou")
        print("ok  rascunho: vai para a caixa de entrada do app e devolve a legenda para copiar")

        # --- erros do TikTok -> o que a fila faz
        rasc = {**item_base, "id": "q3", "arquivo": str(video), "opcoes": {"modo": "rascunho"}}
        dir_ = {**rasc, "opcoes": {"modo": "direto", "privacidade": "SELF_ONLY"}}
        casos = [((403, "unaudited_client_can_only_post_to_private_accounts"), dir_, fila.ErroDefinitivo, "privada"),
                 ((401, "access_token_invalid"), rasc, fila.ErroConta, "expirado"),
                 ((403, "spam_risk_too_many_pending_share"), rasc, fila.ErroCota, "5 rascunhos"),
                 ((403, "spam_risk_too_many_posts"), dir_, fila.ErroCota, "limite diário"),
                 ((429, "rate_limit_exceeded"), rasc, pub_tiktok.ErroTemporario, "rate_limit"),
                 ((500, "internal_error"), rasc, pub_tiktok.ErroTemporario, "")]
        for err, it, tipo, contem in casos:
            falso.erro_init = err
            esperar_erro(tipo, pub_tiktok.publicar, it, conta, cfg, contem=contem)
        falso.erro_init = None
        falso.status = ["FAILED"]
        esperar_erro(fila.ErroDefinitivo, pub_tiktok.publicar, rasc, conta, cfg, contem="rejeitou")
        print("ok  erros: sem auditoria, acesso, limites do dia (reagenda), limite por minuto e falha no processamento")

        # --- validação da janela de publicar
        tt = [conta]
        esperar_erro(ValueError, painel.opcoes_tiktok, {}, tt, contem="rascunho ou direto")
        esperar_erro(ValueError, painel.opcoes_tiktok, {"modo": "direto"}, tt, contem="quem pode ver")
        esperar_erro(ValueError, painel.opcoes_tiktok, {"modo": "direto", "privacidade": "SELF_ONLY", "comercial": True}, tt,
                     contem="promove")
        esperar_erro(ValueError, painel.opcoes_tiktok, {"modo": "direto", "privacidade": "SELF_ONLY", "comercial": True,
                                                        "conteudo_marca": True}, tt, contem="não pode ser privado")
        assert painel.opcoes_tiktok({"modo": "rascunho", "privacidade": "SELF_ONLY"}, tt) == {"modo": "rascunho"}
        op = painel.opcoes_tiktok({"modo": "direto", "privacidade": "MUTUAL_FOLLOW_FRIENDS", "marca_propria": True}, tt)
        assert op["marca_propria"] is False, "sem 'divulgar conteúdo comercial' marcado, os rótulos não valem"
        so_rascunho = {**conta, "modos": ["rascunho"]}
        esperar_erro(ValueError, painel.opcoes_tiktok, {"modo": "direto", "privacidade": "SELF_ONLY"}, [so_rascunho],
                     contem="não autorizou")
        print("ok  janela de publicar: modo, privacidade, conteúdo comercial e permissões validados")

        # --- pela fila, ponta a ponta
        falso.status = ["SEND_TO_USER_INBOX"]
        corte = {"id": "alanzoka/live/longos/0", "arquivo": str(video), "titulo": "Bug", "descricao": "",
                 "hashtags": ["gta6"], "credito": "twitch.tv/alanzoka", "duracao": 95}
        novo = fila.agendar(corte, [conta["id"]], "agora", cfg, privacidade="unlisted", opcoes={"tiktok": {"modo": "rascunho"}})[0]
        assert novo["privacidade"] == "rascunho" and novo["opcoes"] == {"modo": "rascunho"} and novo["duracao"] == 95
        assert fila.executar_vencidos(log=lambda *_: None) == 1
        feito = next(i for i in fila.listar() if i["id"] == novo["id"])
        assert feito["status"] == "publicado" and feito["resultado"]["modo"] == "rascunho", feito
        print("ok  fila: agenda com as opções do TikTok e envia (privacidade do YouTube não vaza para o TikTok)")

        # --- painel: consulta do perfil e início do vínculo
        dados = tmp / "dados"
        dados.mkdir()
        porta = 8820
        threading.Thread(target=painel.servir, args=(dados, porta, False), daemon=True).start()
        time.sleep(0.8)

        def get(caminho):
            con = http.client.HTTPConnection("127.0.0.1", porta, timeout=5)
            con.request("GET", caminho, headers={"Host": f"127.0.0.1:{porta}"})
            r = con.getresponse()
            return r.status, r.getheader("Location"), r.read()

        st, _, corpo = get(f"/api/tiktok/{conta['id']}/criador")
        info = json.loads(corpo)
        assert st == 200 and info["ok"] and info["criador"]["privacidades"] == falso.privacidades
        assert info["criador"]["dueto_off"] is True and info["criador"]["duracao_max"] == 600
        st, local, _ = get("/oauth/tiktok/iniciar")
        assert st == 302 and local.startswith("https://www.tiktok.com/v2/auth/authorize/?client_key=ck_teste")
        estado = json.loads(get("/api/estado")[2])
        assert estado["tiktok_configurado"] and estado["duracao_max"]["tiktok"] == 600 and "SELF_ONLY" in estado["privacidades_tiktok"]
        print("ok  painel: consulta o perfil para a janela e redireciona para o login do TikTok")
    print("\nTudo certo.")


if __name__ == "__main__":
    main()
