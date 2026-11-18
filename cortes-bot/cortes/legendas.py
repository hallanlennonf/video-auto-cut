"""Legendas no estilo Shorts/TikTok: poucas palavras por vez, MAIÚSCULAS, a palavra falada destacada."""
from pathlib import Path

CABECALHO = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Fala,Arial Black,78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

DESTAQUE = r"{\c&H00D7FF&}"   # amarelo (BGR)
NORMAL = r"{\c&HFFFFFF&}"


def _t(s: float) -> str:
    s = max(0.0, s)
    h, r = divmod(s, 3600)
    m, r = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{r:05.2f}"


def _grupos(palavras: list[dict], max_palavras: int = 3, max_chars: int = 18, pausa: float = 0.6) -> list[list[dict]]:
    grupos, atual = [], []
    for p in palavras:
        quebra = atual and (
            len(atual) >= max_palavras
            or len(" ".join(x["w"] for x in atual + [p])) > max_chars
            or p["s"] - atual[-1]["e"] > pausa
            or atual[-1]["w"][-1:] in ".!?"
        )
        if quebra:
            grupos.append(atual)
            atual = []
        atual.append(p)
    if atual:
        grupos.append(atual)
    return grupos


def gerar_ass(palavras: list[dict], inicio: float, fim: float, destino: Path, y: int) -> Path | None:
    """Gera um .ass com tempos relativos ao início do corte. `y` = altura do centro da legenda."""
    dentro = [p for p in palavras if p["e"] > inicio and p["s"] < fim]
    if not dentro:
        return None
    eventos = []
    for g in _grupos(dentro):
        fim_grupo = g[-1]["e"]
        for i, p in enumerate(g):
            ate = g[i + 1]["s"] if i + 1 < len(g) else fim_grupo + 0.15
            partes = [(DESTAQUE if j == i else NORMAL) + x["w"].upper().replace("{", "").replace("}", "")
                      for j, x in enumerate(g)]
            txt = rf"{{\pos(540,{y})}}" + " ".join(partes)
            eventos.append([p["s"], min(ate, fim), txt])
    # a folga de 0,15s da ultima palavra de um grupo nao pode invadir o grupo seguinte:
    # como todos usam o mesmo \pos, dois eventos ao mesmo tempo sao desenhados um sobre o outro
    for atual, proximo in zip(eventos, eventos[1:]):
        atual[1] = min(atual[1], proximo[0])
    linhas = [f"Dialogue: 0,{_t(ini - inicio)},{_t(f - inicio)},Fala,,0,0,0,,{txt}"
              for ini, f, txt in eventos if f > ini]
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(CABECALHO + "\n".join(linhas) + "\n", encoding="utf-8")
    return destino
