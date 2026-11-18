"""Gera o corte vertical 1080x1920 com ffmpeg.

- Com facecam configurada: facecam em cima (1080x640) + jogo embaixo (1080x1280).
- Sem facecam: vídeo inteiro centralizado sobre um fundo desfocado do próprio vídeo.
- Crédito do streamer sempre na tela.
"""
import re
import subprocess
from pathlib import Path

from . import config

FONTE = "C\\:/Windows/Fonts/arialbd.ttf"


def _texto_ffmpeg(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace("%", "\\%")


def posicao_legenda(facecam: dict | None) -> int:
    """Centro vertical da legenda: na emenda câmera/jogo, ou no terço inferior sem facecam."""
    return 640 if facecam else 1380


def _filtro(facecam: dict | None, credito: str, legenda: str | None = None) -> str:
    cred = (
        f"drawtext=fontfile='{FONTE}':text='{_texto_ffmpeg(credito)}':fontsize=44:fontcolor=white:"
        "borderw=4:bordercolor=black@0.85:x=(w-text_w)/2:y=h-200"
    )
    if legenda:
        cred = f"subtitles=filename='{legenda}',{cred}"
    if facecam:
        fx, fy, fw, fh = (facecam[k] for k in ("x", "y", "w", "h"))
        return (
            f"[0:v]split=2[a][b];"
            f"[a]crop={fw}:{fh}:{fx}:{fy},scale=1080:640:force_original_aspect_ratio=increase,crop=1080:640[cam];"
            f"[b]crop=ih*1080/1280:ih,scale=1080:1280[jogo];"
            f"[cam][jogo]vstack=inputs=2,setsar=1,{cred}[v]"
        )
    return (
        f"[0:v]split=2[a][b];"
        f"[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=30,eq=brightness=-0.15[fundo];"
        f"[b]scale=1080:-2[frente];"
        f"[fundo][frente]overlay=(W-w)/2:(H-h)/2,setsar=1,{cred}[v]"
    )


def fracao_parada(video: Path, inicio: float, fim: float) -> float:
    """Fração do trecho em que a imagem fica congelada (tela de pausa, 'volto já', cena estática)."""
    cmd = [
        config.ffmpeg(), "-ss", f"{inicio:.2f}", "-to", f"{fim:.2f}", "-i", str(video),
        "-vf", "scale=320:-2,freezedetect=n=0.003:d=1", "-an", "-f", "null", "-",
    ]
    err = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    parado = sum(float(x) for x in re.findall(r"freeze_duration: ([0-9.]+)", err))
    inicios = re.findall(r"freeze_start: ([0-9.]+)", err)
    if len(inicios) > err.count("freeze_end"):  # congelado até o fim do trecho
        parado += max(0.0, (fim - inicio) - float(inicios[-1]))
    return min(1.0, parado / max(fim - inicio, 1e-6))


def concatenar(partes: list[Path], saida: Path) -> Path:
    """Junta cortes verticais já renderizados (mesmo formato) num único vídeo."""
    saida = saida.resolve()
    saida.parent.mkdir(parents=True, exist_ok=True)
    entradas = []
    for p in partes:
        entradas += ["-i", str(Path(p).resolve())]
    rotulos = "".join(f"[{i}:v][{i}:a]" for i in range(len(partes)))
    cmd = [
        config.ffmpeg(), "-y", "-v", "error", *entradas,
        "-filter_complex", f"{rotulos}concat=n={len(partes)}:v=1:a=1[v][a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart",
        str(saida),
    ]
    subprocess.run(cmd, check=True)
    return saida


def renderizar(video: Path, inicio: float, fim: float, saida: Path, canal: dict, legenda: Path | None = None) -> Path:
    """`legenda`: arquivo .ass opcional (tempos relativos ao início do corte)."""
    saida = saida.resolve()
    saida.parent.mkdir(parents=True, exist_ok=True)
    # o ffmpeg roda dentro da pasta da legenda: assim o filtro recebe só o nome do arquivo,
    # sem os ":" e "\" dos caminhos do Windows (que quebram a sintaxe dos filtros)
    pasta = legenda.resolve().parent if legenda else None
    cmd = [
        config.ffmpeg(), "-y", "-v", "error",
        "-ss", f"{inicio:.2f}", "-to", f"{fim:.2f}", "-i", str(Path(video).resolve()),
        "-filter_complex", _filtro(canal.get("facecam"), canal.get("credito", ""), legenda.name if legenda else None),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart",
        str(saida),
    ]
    subprocess.run(cmd, check=True, cwd=pasta)
    return saida
