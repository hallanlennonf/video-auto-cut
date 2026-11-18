"""Painel local de revisão e publicação: http://localhost:8800

- Revisão: assiste aos cortes de todos os canais, edita título/descrição/hashtags, aprova ou rejeita.
- Ao aprovar: escolhe em quais contas publicar e quando (próximo horário livre, agora ou data/hora).
- Fila: acompanha, cancela, adianta ou tenta de novo cada publicação.
- Contas: vincula canais do YouTube (login no site do Google) e define as contas padrão de cada streamer.

Só escuta em 127.0.0.1 e recusa pedidos vindos de outros sites (checagem de Host/Origin), para que
uma página qualquer aberta no navegador não consiga publicar nada em seu nome.
"""
import html as html_mod
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import config, contas, fila

_trava = threading.Lock()
SEGURO = re.compile(r"^[\w.-]+$")
LISTAS = {"candidatos": "curto", "longos": "tiktok", "compilacoes": "compilacao"}


# ------------------------------------------------------------------ cortes

def _arquivos_json(dados: Path) -> list[Path]:
    return sorted(dados.glob("*/candidatos/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def listar(dados: Path) -> list[dict]:
    publicacoes: dict[str, list] = {}
    for item in fila.listar():
        if item["status"] != "cancelado":
            publicacoes.setdefault(item["corte_id"], []).append(
                {"conta": item["conta_nome"], "conta_id": item["conta_id"], "plataforma": item["plataforma"], "status": item["status"],
                 "quando": item["agendado_para"], "url": (item.get("resultado") or {}).get("url")})
    saida = []
    for arq in _arquivos_json(dados):
        canal, live = arq.parent.parent.name, arq.stem
        d = json.loads(arq.read_text(encoding="utf-8"))
        textos = {c.get("id"): c.get("texto", "") for c in d.get("candidatos", [])}
        for lista, formato in LISTAS.items():
            for i, c in enumerate(d.get(lista) or []):
                if not c.get("arquivo") or not config.absoluto(c["arquivo"]).exists():
                    continue
                ia = c.get("ia") or {}
                rev = c.get("revisao") or {}
                texto = c.get("texto", "") if lista != "compilacoes" else " … ".join(textos.get(p, "") for p in c["partes"])
                ident = f"{canal}/{live}/{lista}/{i}"
                saida.append({
                    "id": ident, "canal": canal, "live": live.removesuffix("_audio"),
                    "formato": formato, "inicio": c["inicio"],
                    "duracao": round(c.get("duracao") or (c["fim"] - c["inicio"])),
                    "partes": len(c.get("partes", [])),
                    "nota": c.get("nota"), "nota_ia": ia.get("nota"), "motivo_ia": ia.get("motivo", ""),
                    "texto": texto,
                    "titulo": rev.get("titulo", ia.get("titulo", "")),
                    "descricao": rev.get("descricao", ia.get("descricao", "")),
                    "hashtags": rev.get("hashtags", ia.get("hashtags", [])),
                    "status": rev.get("status", "pendente"),
                    "publicacoes": publicacoes.get(ident, []),
                })
    return saida


def _localizar(dados: Path, ident: str) -> tuple[Path, str, int]:
    canal, live, lista, idx = ident.split("/")
    if not (SEGURO.match(canal) and SEGURO.match(live) and lista in LISTAS and idx.isdigit()):
        raise ValueError("id inválido")
    return dados / canal / "candidatos" / f"{live}.json", lista, int(idx)


def atualizar(dados: Path, ident: str, mudancas: dict) -> dict:
    arq, lista, idx = _localizar(dados, ident)
    with _trava:
        d = json.loads(arq.read_text(encoding="utf-8"))
        c = d[lista][idx]
        rev = c.setdefault("revisao", {})
        for k in ("titulo", "descricao", "status"):
            if k in mudancas:
                rev[k] = str(mudancas[k])[:500]
        if "hashtags" in mudancas:
            rev["hashtags"] = [str(h).strip().lstrip("#") for h in mudancas["hashtags"] if str(h).strip()][:10]
        if rev.get("status") not in ("pendente", "aprovado", "rejeitado"):
            rev["status"] = "pendente"
        tmp = arq.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(arq)
        return c


def caminho_video(dados: Path, ident: str) -> Path:
    arq, lista, idx = _localizar(dados, ident)
    c = json.loads(arq.read_text(encoding="utf-8"))[lista][idx]
    p = config.absoluto(c["arquivo"]).resolve()
    if dados.resolve() not in p.parents:
        raise ValueError("fora da pasta de dados")
    return p


def publicar(dados: Path, pedido: dict) -> list[dict]:
    """Aprova o corte (salvando título/descrição/hashtags) e agenda nas contas escolhidas."""
    cfg = config.carregar()
    ident = pedido["corte_id"]
    if not str(pedido.get("titulo", "")).strip():
        raise ValueError("escreva um título antes de publicar")
    if not pedido.get("contas"):
        raise ValueError("escolha pelo menos uma conta")
    if pedido.get("modo") not in ("proximo", "agora", "data"):
        raise ValueError("modo de agendamento inválido")
    if pedido["modo"] == "data" and not pedido.get("data"):
        raise ValueError("escolha a data e hora")
    c = atualizar(dados, ident, {"status": "aprovado", "titulo": pedido["titulo"],
                                 "descricao": pedido.get("descricao", ""), "hashtags": pedido.get("hashtags", [])})
    rev = c["revisao"]
    canal = cfg["canais"].get(ident.split("/")[0], {})
    duracao = c.get("duracao") or (c["fim"] - c["inicio"])
    for cid in pedido["contas"]:
        conta = contas.obter(cid)
        if conta is None:
            raise ValueError("conta não encontrada")
        if not contas.compativel(conta, duracao):
            raise ValueError(f"{conta['nome']} não aceita vídeos de {duracao:.0f}s")
    corte = {"id": ident, "arquivo": c["arquivo"], "titulo": rev["titulo"], "descricao": rev.get("descricao", ""),
             "hashtags": rev.get("hashtags", []), "credito": canal.get("credito", "")}
    privacidade = pedido.get("privacidade") if pedido.get("privacidade") in ("public", "unlisted", "private") else None
    return fila.agendar(corte, pedido["contas"], pedido["modo"], cfg, pedido.get("data"), privacidade)


def estado() -> dict:
    from . import pub_youtube

    cfg = config.carregar()
    return {
        "canais": {n: {"destino": c.get("canal_destino", n), "credito": c.get("credito", "")} for n, c in cfg["canais"].items()},
        "contas": contas.listar(),
        "plataformas": {k: v["nome"] for k, v in contas.PLATAFORMAS.items()},
        "youtube_configurado": pub_youtube.configurado(cfg),
        "youtube_credencial": cfg["publicacao"]["youtube"]["client_secret"],
        "horarios": cfg["publicacao"]["horarios"], "fuso": cfg["publicacao"]["fuso"],
        "max_por_dia": cfg["publicacao"]["max_por_dia_por_conta"],
        "privacidade_youtube": cfg["publicacao"]["youtube"].get("privacidade", "public"),
    }


# ------------------------------------------------------------------ servidor

def servir(dados: Path, porta: int = 8800, trabalhador: bool = True) -> None:
    from . import pub_youtube

    pagina = Path(__file__).parent / "painel.html"
    hosts_ok = {f"localhost:{porta}", f"127.0.0.1:{porta}"}
    origens_ok = {f"http://{h}" for h in hosts_ok}

    class H(BaseHTTPRequestHandler):
        def _json(self, obj, status=200):
            corpo = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)

        def _html(self, texto: str, status=200):
            corpo = texto.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def _redirecionar(self, url: str):
            self.send_response(302)
            self.send_header("Location", url)
            self.end_headers()

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in hosts_ok  # bloqueia DNS rebinding

        def _mensagem(self, titulo: str, texto: str, status=200):
            self._html(f"<!doctype html><meta charset=utf-8><title>{html_mod.escape(titulo)}</title>"
                       f"<body style='font:16px system-ui;max-width:640px;margin:40px auto;padding:0 16px'>"
                       f"<h1 style='font-size:20px'>{html_mod.escape(titulo)}</h1><p>{texto}</p>"
                       f"<p><a href='/#contas'>Voltar ao painel</a></p>", status)

        def do_GET(self):
            if not self._host_ok():
                return self.send_error(403)
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    self._html(pagina.read_text(encoding="utf-8"))
                elif url.path == "/api/cortes":
                    self._json(listar(dados))
                elif url.path == "/api/estado":
                    self._json(estado())
                elif url.path == "/api/fila":
                    self._json(sorted(fila.listar(), key=lambda i: i["agendado_para"], reverse=True))
                elif url.path.startswith("/video/"):
                    self._video(caminho_video(dados, unquote(url.path[len("/video/"):])))
                elif url.path == "/oauth/youtube/iniciar":
                    cfg = config.carregar()
                    if not pub_youtube.configurado(cfg):
                        return self._mensagem("Falta a credencial do YouTube",
                                              "Siga o passo a passo do README para criar a credencial no Google Cloud "
                                              f"e salve o arquivo em <code>{html_mod.escape(cfg['publicacao']['youtube']['client_secret'])}</code>.", 400)
                    self._redirecionar(pub_youtube.iniciar_vinculo(cfg, porta))
                elif url.path == "/oauth/youtube/callback":
                    q = parse_qs(url.query)
                    if "error" in q:
                        return self._mensagem("Vínculo cancelado", html_mod.escape(q["error"][0]))
                    conta = pub_youtube.concluir_vinculo(q.get("state", [""])[0], q.get("code", [""])[0])
                    self._mensagem("Canal vinculado", f"<b>{html_mod.escape(conta['nome'])}</b> foi vinculado. "
                                   "Agora escolha, na aba Contas, para quais streamers ele é conta padrão.")
                else:
                    self.send_error(404)
            except (ValueError, KeyError, IndexError, FileNotFoundError) as e:
                if url.path.startswith("/oauth/"):
                    self._mensagem("Não deu certo", html_mod.escape(str(e)), 400)
                else:
                    self.send_error(404)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass

        def do_POST(self):
            # Só aceita JSON vindo do próprio painel: outro site não consegue enviar
            # application/json para cá sem o navegador bloquear (CORS), e o Origin é conferido.
            origem = self.headers.get("Origin")
            if (not self._host_ok() or (origem and origem not in origens_ok)
                    or not self.headers.get("Content-Type", "").startswith("application/json")):
                return self.send_error(403)
            caminho = unquote(urlparse(self.path).path)
            try:
                tamanho = int(self.headers.get("Content-Length", 0))
                corpo = json.loads(self.rfile.read(tamanho) or b"{}")
                if caminho.startswith("/api/cortes/"):
                    atualizar(dados, caminho[len("/api/cortes/"):], corpo)
                    self._json({"ok": True})
                elif caminho == "/api/publicar":
                    itens = publicar(dados, corpo)
                    self._json({"ok": True, "itens": itens})
                elif caminho == "/api/contas/simulacao":
                    nome = str(corpo.get("nome") or "").strip()[:60] or "Conta de teste"
                    self._json({"ok": True, "conta": contas.adicionar("simulacao", nome)})
                elif m := re.fullmatch(r"/api/contas/([\w-]+)/remover", caminho):
                    contas.remover(m.group(1))
                    self._json({"ok": True})
                elif m := re.fullmatch(r"/api/contas/([\w-]+)", caminho):
                    self._json({"ok": True, "conta": contas.atualizar(m.group(1), canais=corpo.get("canais", []))})
                elif m := re.fullmatch(r"/api/fila/([\w-]+)/(cancelar|tentar|agora)", caminho):
                    self._json({"ok": True, "item": fila.acao(m.group(1), m.group(2))})
                else:
                    self.send_error(404)
            except (ValueError, KeyError, IndexError, FileNotFoundError, StopIteration, RuntimeError) as e:
                self._json({"ok": False, "erro": str(e) or "pedido inválido"}, 400)

        def _video(self, p: Path):
            """Serve o mp4 com suporte a Range (necessário para o player avançar/voltar)."""
            total = p.stat().st_size
            ini, fim = 0, total - 1
            faixa = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if faixa and (faixa.group(1) or faixa.group(2)):
                if faixa.group(1):
                    ini = int(faixa.group(1))
                    fim = int(faixa.group(2)) if faixa.group(2) else total - 1
                else:
                    ini = max(0, total - int(faixa.group(2)))
                fim = min(fim, total - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {ini}-{fim}/{total}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(fim - ini + 1))
            self.end_headers()
            with open(p, "rb") as f:
                f.seek(ini)
                falta = fim - ini + 1
                while falta > 0:
                    bloco = f.read(min(1 << 20, falta))
                    if not bloco:
                        break
                    self.wfile.write(bloco)
                    falta -= len(bloco)

        def log_message(self, *_):
            pass

    if trabalhador:
        fila.iniciar_trabalhador()
        print("Fila de publicação: ativa (publica nos horários agendados enquanto o painel estiver aberto)")
    print(f"Painel em http://localhost:{porta}  (Ctrl+C para parar)")
    ThreadingHTTPServer(("127.0.0.1", porta), H).serve_forever()


def principal(porta: int = 8800) -> None:
    servir(config.RAIZ / config.carregar()["pasta_dados"], porta)
