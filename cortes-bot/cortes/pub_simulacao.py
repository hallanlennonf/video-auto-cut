"""Publicador de simulação: não envia nada para lugar nenhum. Serve para testar a fila e o painel."""
import time
from pathlib import Path


def publicar(item: dict, conta: dict, cfg: dict) -> dict:
    time.sleep(1.5)
    tamanho = Path(item["arquivo"]).stat().st_size
    return {"video_id": f"sim-{item['id']}", "url": f"simulacao://{conta['nome']}/{item['id']}",
            "privacidade": item["privacidade"], "bytes": tamanho}
