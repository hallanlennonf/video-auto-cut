# Bot de cortes — fase 1 (MVP local)

Um canal de destino por streamer: **alanzoka** (Twitch), **paulinholokobr** e **coringa** (Kick).
Configuração em `config.yaml`.

## Instalação

Na pasta `D:\video-auto-cut` (o ambiente fica em `D:\video-auto-cut\.venv`):

```bash
python -m venv .venv
.venv\Scripts\pip install -r cortes-bot\requirements.txt
```

Os comandos abaixo rodam de dentro de `cortes-bot`, com o Python do ambiente:
`..\.venv\Scripts\python -m cortes ...`

Use um caminho curto para o ambiente: o Windows quebra a instalação do pip em caminhos com mais de 260 caracteres.
O ffmpeg vem junto pelo pacote `imageio-ffmpeg`, sem instalar nada no sistema.

## Fluxo

1. **Gravar o chat durante a live** (deixe rodando):
   - Twitch: `python -m cortes chat-twitch` (IRC oficial, anônimo, só leitura)
   - Kick: `python -m cortes kick-assinar` uma vez, e depois `python -m cortes kick-webhook`
     (API oficial: precisa de um app em kick.com/settings/developer + um túnel público até a porta 8787)
2. **Depois da live**, listar os VODs e baixar **só o áudio** (cerca de 600 MB para 7h de live, em cerca de 1 min):
   `python -m cortes vods alanzoka` → `python -m cortes baixar-audio alanzoka <url>`
3. **Processar**: `python -m cortes processar alanzoka dados/alanzoka/vods/<id>_audio.mp4`
   - Detecta até 30 momentos pelo áudio (+ chat, se gravado)
   - **Whisper** transcreve cada um na GPU, dá nota à fala (reações, risadas, exclamações, ritmo),
     ajusta início e fim para não cortar frases no meio e escolhe os 10 melhores
   - Baixa em 1080p **só os trechos** escolhidos e queima **legendas palavra por palavra**
     (na emenda câmera/jogo); a transcrição fica salva no .json para gerar títulos depois
   - `--sem-whisper` pula a transcrição (mais rápido, sem legendas)
   - **IA (Claude)**: lê as transcrições de todos os candidatos juntos, reprova o que não funciona
     sozinho (pedido de like, conversa morta, música de pausa), apara a enrolação do começo/fim e
     escreve título, descrição e hashtags. Uma chamada por live (`--sem-ia` desliga).
     Precisa da variável `ANTHROPIC_API_KEY` (chave em platform.claude.com); sem ela, o bot pula esta etapa.
   - **Duas versões por live** (`--sem-tiktok` gera só a primeira):

     | Versão | Duração | Pasta | Para |
     |---|---|---|---|
     | Curtos | 20–60s, 1 momento | `cortes/curtos/` | YouTube Shorts, Reels |
     | TikTok longo | 65–120s, atividade sustentada | `cortes/tiktok/*_longo_*` | TikTok (monetiza só > 1 min) |
     | TikTok compilação | 65–120s, 2–4 curtos do mesmo tema | `cortes/tiktok/*_compilacao_*` | TikTok |

     Com IA, ela agrupa as compilações por tema e dá título; sem IA, sai uma compilação
     "melhores momentos" com os curtos de maior nota, na ordem da live. Ajustes na seção `tiktok:` do config.
4. **Revisar**: `python -m cortes painel` e abra http://localhost:8800 (filtro por formato)
   - Assista a cada corte, ajuste título/descrição/hashtags e aprove ou rejeite
   - Fica salvo no `.json` da live (campo `revisao`), pronto para a fase de publicação
   - Descarta sozinho as telas de pausa ("volto já" com música), detectando imagem parada
   - Gera `dados/<canal>/candidatos/<id>.json` e os cortes verticais em `dados/<canal>/cortes/`
   - `--sem-render` só lista os momentos, sem baixar nem gerar vídeo (bom para calibrar)
   - Ainda dá para baixar o VOD inteiro com `baixar` (cerca de 20 GB em 1080p) e processar o .mp4 completo

O replay do chat de VODs antigos da Twitch não está disponível (a biblioteca `chat-downloader` trava).
Por isso o chat precisa ser gravado **durante** a live com `chat-twitch`.

5. **Publicar**: ainda no painel, clique em **Aprovar…** num corte. Na janela, escolha as contas
   (as contas padrão do streamer já vêm marcadas) e quando publicar:
   - **Próximo horário livre**: usa `publicacao.horarios` do config (até 4 por dia por conta, um por horário),
     respeitando a cota diária do YouTube
   - **Agora** ou **numa data/hora** específica
   - A aba **Fila** mostra cada envio (agendado → enviando → publicado/erro) com o link do vídeo;
     dá para publicar agora, cancelar ou reagendar
   - **O painel precisa estar aberto** (`python -m cortes painel`) no horário agendado: é ele que publica.
     Se o PC estiver desligado, o que venceu sai assim que o painel abrir de novo
   - Cota do YouTube esgotada → reagenda sozinho para o dia seguinte; acesso expirado → a conta fica
     marcada para "vincular de novo" na aba Contas

## Vincular o YouTube (uma vez)

O painel publica pela API oficial do YouTube. Para isso o Google exige uma credencial sua:

1. Entre em https://console.cloud.google.com e crie um projeto (ex.: `cortes-bot`).
2. **APIs e serviços → Biblioteca**: procure **YouTube Data API v3** e clique em **Ativar**.
3. **Google Auth Platform / Tela de permissão OAuth**: tipo **Externo**; preencha nome do app e e-mail.
   Em **Público-alvo**, adicione os e-mails das contas Google dos seus canais como usuários de teste.
   - Em modo **Teste**, o Google expira o acesso a cada **7 dias** (você teria de vincular de novo toda semana).
     Para evitar, mude o status para **Em produção**: sem verificação, o Google só mostra um aviso de
     "app não verificado" no login, o que é suficiente para uso próprio.
4. **Credenciais → Criar credenciais → ID do cliente OAuth → Aplicativo da Web**.
   Em **URIs de redirecionamento autorizados**, coloque exatamente:
   `http://localhost:8800/oauth/youtube/callback`
5. Baixe o JSON e salve como `segredos/youtube_client_secret.json` (dentro desta pasta).
6. Abra o painel → aba **Contas** → **Vincular canal do YouTube**, faça login no Google e escolha o canal.
   Se aparecer "o Google não verificou este app", clique em **Avançado → Acessar** (é o seu próprio app).
   Repita para cada canal; depois marque de quais streamers cada canal é a conta padrão.

Limites do YouTube enquanto o projeto não passa pela auditoria do Google:
- **Os vídeos enviados pela API ficam privados.** Torne-os públicos no YouTube Studio, ou peça a
  auditoria no formulário "YouTube API Services – Audit and Quota Extension".
- **Cota: ~6 envios por dia somando todos os canais do projeto** (10.000 unidades; cada envio gasta 1.600).
  A mesma auditoria serve para pedir mais cota. O agendador já respeita esse limite (`publicacao.youtube.envios_por_dia`).

Os acessos ficam só no seu PC, em `dados/_publicacao/tokens/`. **Nunca compartilhe essa pasta nem `segredos/`.**

## Antes de usar de verdade

- **Facecam**: meça a posição da câmera de cada streamer (num print do VOD em 1920x1080) e preencha
  `facecam: {x, y, w, h}` no config. Sem isso, o corte usa o layout de fundo desfocado.
- **Calibração**: rode com `--sem-render` num VOD real, confira os horários e ajuste
  `nota_minima`, `atraso_chat` e os pesos.
- **Permissão** dos streamers para cortar e monetizar.

## Teste

`python tests/teste_sintetico.py` gera uma live falsa de 10 minutos, com momentos conhecidos, e valida a detecção e os cortes 1080x1920.

## Próximas fases

2. ~~Whisper, legendas, IA para título/filtro e painel de revisão~~ (feito)
3. Upload pelas APIs oficiais (YouTube → TikTok → Instagram)
4. Métricas e relatórios
