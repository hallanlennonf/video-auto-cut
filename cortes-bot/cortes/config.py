from pathlib import Path

import imageio_ffmpeg
import yaml

RAIZ = Path(__file__).resolve().parent.parent


def carregar(caminho: str | Path = RAIZ / "config.yaml") -> dict:
    with open(caminho, encoding="utf-8") as f:
        return yaml.safe_load(f)


def canal(cfg: dict, nome: str) -> dict:
    try:
        return cfg["canais"][nome]
    except KeyError:
        raise SystemExit(f"Canal '{nome}' não está no config.yaml. Opções: {', '.join(cfg['canais'])}")


def pasta_canal(cfg: dict, nome: str) -> Path:
    p = RAIZ / cfg["pasta_dados"] / nome
    for sub in ("vods", "chat", "candidatos", "cortes"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    return p


def relativo(p: str | Path) -> str:
    """Caminho para guardar nos .json: relativo à pasta do projeto, para sobreviver se ela mudar de lugar."""
    p = Path(p).resolve()
    try:
        return p.relative_to(RAIZ.resolve()).as_posix()
    except ValueError:
        return str(p)


def absoluto(s: str | Path) -> Path:
    """Inverso de relativo(): caminhos relativos são lidos a partir da pasta do projeto."""
    p = Path(s)
    return p if p.is_absolute() else RAIZ / p


def ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()
