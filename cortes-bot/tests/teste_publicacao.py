"""Teste da publicação sem publicar nada de verdade: agendamento, fila, erros, corpo do YouTube e
segurança do painel. Usa uma pasta temporária (não mexe nas suas contas/fila reais).

  python tests/teste_publicacao.py
"""
import http.client
import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cortes import config, contas, fila, painel, pub_youtube  # noqa: E402

SP = ZoneInfo("America/Sao_Paulo")


def local(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=SP)


def item(conta_id: str, quando: datetime, plataforma="simulacao", status="agendado") -> dict:
    return {"id": f"x{quando.timestamp()}{conta_id}", "conta_id": conta_id, "plataforma": plataforma,
            "status": status, "agendado_para": quando.astimezone(timezone.utc).isoformat()}


def main():
    cfg = config.carregar()
    cfg["publicacao"].update(fuso="America/Sao_Paulo", horarios=["11:00", "14:00", "18:00", "21:00"], max_por_dia_por_conta=4)
    cfg["publicacao"]["youtube"]["envios_por_dia"] = 6
    a = {"id": "a", "plataforma": "simulacao"}

    # --- agendamento
    t = fila.proximo_horario(a, [], cfg, depois=local("2026-11-18T12:00"))
    assert t.astimezone(SP) == local("2026-11-18T14:00"), t
    t = fila.proximo_horario(a, [item("a", local("2026-11-18T14:00"))], cfg, depois=local("2026-11-18T12:00"))
    assert t.astimezone(SP) == local("2026-11-18T18:00"), t
    cheio = [item("a", local(f"2026-11-18T{h}")) for h in ("11:00", "14:00", "18:00", "21:00")]
    t = fila.proximo_horario(a, cheio, cfg, depois=local("2026-11-18T09:00"))
    assert t.astimezone(SP) == local("2026-11-19T11:00"), t
    t = fila.proximo_horario(a, [item("b", local("2026-11-18T14:00"))], cfg, depois=local("2026-11-18T12:00"))
    assert t.astimezone(SP) == local("2026-11-18T14:00"), "outra conta no mesmo horário não deveria bloquear"
    print("ok  próximo horário livre (horários, limite diário, contas independentes)")

    # cota do YouTube: 6 envios por dia do Pacífico, somando todas as contas do projeto
    y1, y2 = {"id": "y1", "plataforma": "youtube"}, {"id": "y2", "plataforma": "youtube"}
    seis = [item("y1", local(f"2026-11-18T{h}"), "youtube") for h in ("11:00", "14:00", "18:00")] + \
           [item("y2", local(f"2026-11-18T{h}"), "youtube") for h in ("11:00", "14:00", "18:00")]
    t = fila.proximo_horario(y2, seis, cfg, depois=local("2026-11-18T10:00"))
    # 21:00 em SP ainda é 18/11 no Pacífico (cota cheia); 19/11 11:00 SP já é outro dia lá
    assert t.astimezone(SP) == local("2026-11-19T11:00"), t
    print("ok  cota diária do YouTube respeitada entre canais")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "tokens").mkdir()
        contas.pasta = lambda: tmp  # isola: não toca nas contas/fila reais
        video = tmp / "corte.mp4"
        video.write_bytes(b"\x00" * 1000)

        sim = contas.adicionar("simulacao", "Teste alanzoka")
        contas.atualizar(sim["id"], canais=["alanzoka", "nao-existe"])
        assert contas.obter(sim["id"])["canais"] == ["alanzoka"]
        corte = {"id": "alanzoka/live/candidatos/0", "arquivo": str(video), "titulo": "Bug bizarro", "descricao": "d",
                 "hashtags": ["gta6"], "credito": "twitch.tv/alanzoka"}
        novos = fila.agendar(corte, [sim["id"]], "agora", cfg)
        assert len(novos) == 1 and fila.agendar(corte, [sim["id"]], "agora", cfg) == [], "não deve duplicar"
        assert fila.executar_vencidos(log=lambda *_: None) == 1
        pub = fila.listar()[0]
        assert pub["status"] == "publicado" and pub["resultado"]["url"].startswith("simulacao://"), pub
        print("ok  fila: agenda, não duplica, publica (simulação)")

        futuro = fila.agendar({**corte, "id": "alanzoka/live/candidatos/1"}, [sim["id"]], "proximo", cfg)[0]
        assert fila._dt(futuro["agendado_para"]) > datetime.now(timezone.utc)
        assert fila.executar_vencidos(log=lambda *_: None) == 0, "item futuro não pode sair antes da hora"
        assert fila.acao(futuro["id"], "cancelar")["status"] == "cancelado"
        assert fila.acao(futuro["id"], "agora")["status"] == "agendado"
        print("ok  item futuro espera o horário; cancelar e 'publicar agora' funcionam")

        original = fila._publicador
        try:
            def cota(*_):
                raise fila.ErroCota("quotaExceeded")
            fila._publicador = lambda plat: cota
            fila.executar_vencidos(log=lambda *_: None)
            it = next(i for i in fila.listar() if i["id"] == futuro["id"])
            assert it["status"] == "agendado" and "cota" in it["erro"] and fila._dt(it["agendado_para"]) > datetime.now(timezone.utc), it
            print("ok  cota esgotada: reagenda para o dia seguinte")

            def expirou(*_):
                raise fila.ErroConta("invalid_grant")
            fila._publicador = lambda plat: expirou
            fila.acao(futuro["id"], "agora")
            fila.executar_vencidos(log=lambda *_: None)
            it = next(i for i in fila.listar() if i["id"] == futuro["id"])
            assert it["status"] == "erro" and contas.obter(sim["id"])["precisa_reconectar"], it
            print("ok  acesso expirado: marca a conta para reconectar")
        finally:
            fila._publicador = original

        # --- corpo do envio ao YouTube
        corpo = pub_youtube.montar_corpo({"titulo": "Morri <de novo> " + "x" * 120, "descricao": "Fuga da polícia",
                                          "hashtags": ["#gta6", "alanzoka", "cortes do alan"],
                                          "credito": "twitch.tv/alanzoka", "privacidade": "unlisted"}, cfg)
        s = corpo["snippet"]
        assert "<" not in s["title"] and len(s["title"]) <= 100
        assert s["tags"] == ["gta6", "alanzoka", "cortes do alan"]
        assert "Créditos: twitch.tv/alanzoka" in s["description"] and "#gta6 #alanzoka #cortesdoalan" in s["description"]
        assert corpo["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False, "embeddable": True}
        print("ok  corpo do YouTube: título limpo, tags, créditos, hashtags e visibilidade")

        # --- segurança do painel
        dados = tmp / "dados"
        dados.mkdir()
        porta = 8819
        threading.Thread(target=painel.servir, args=(dados, porta, False), daemon=True).start()
        time.sleep(0.8)

        def post(caminho, corpo, **hdr):
            con = http.client.HTTPConnection("127.0.0.1", porta, timeout=5)
            cab = {"Host": f"127.0.0.1:{porta}", "Content-Type": "application/json", **hdr}
            con.request("POST", caminho, body=json.dumps(corpo), headers=cab)
            r = con.getresponse()
            return r.status, r.read()

        assert post("/api/contas/simulacao", {"nome": "ok"})[0] == 200
        assert post("/api/contas/simulacao", {}, **{"Content-Type": "text/plain"})[0] == 403
        assert post("/api/contas/simulacao", {}, Origin="https://site-malicioso.com")[0] == 403
        assert post("/api/contas/simulacao", {}, Host="malicioso.com:8819")[0] == 403
        st, corpo = post("/api/publicar", {"corte_id": "x/y/candidatos/0", "contas": [], "modo": "agora", "titulo": "t"})
        assert st == 400 and "conta" in json.loads(corpo)["erro"]
        print("ok  painel recusa pedidos de outros sites (Content-Type, Origin, Host) e valida o pedido")
    print("\nTudo certo.")


if __name__ == "__main__":
    main()
