# ================================
# worker.py
# Corre dentro do GitHub Actions. Recebe o utilizador e o link da playlist
# como argumentos (vindos do Apps Script), descarrega tudo via yt-dlp + LRCLIB,
# e envia para o Google Drive do utilizador.
#
# A pasta do utilizador está configurada como "qualquer pessoa com o link
# pode editar", por isso a Service Account consegue aceder-lhe só pelo ID,
# sem precisar de ser convidada explicitamente.
# ================================

# ================================
# worker.py (versão atualizada — tracking em Drive, não em Sheet)
# ================================

import sys
import os
import re
import json
import time
import unicodedata
import requests
import yt_dlp
from mutagen.id3 import ID3, COMM
from mutagen.mp3 import MP3
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
import gspread
import io

# --------------------------------
# 1. Argumentos recebidos (utilizador + link da playlist)
# --------------------------------
UTILIZADOR_ATUAL = sys.argv[1]
PLAYLIST_URL = sys.argv[2]

# --------------------------------
# 2. Autenticação com a Service Account
# --------------------------------
credenciais_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
creds = service_account.Credentials.from_service_account_info(credenciais_json, scopes=SCOPES)

drive_service = build("drive", "v3", credentials=creds)
gc = gspread.authorize(creds)

SHEET_ID = os.environ["SHEET_ID"]
sh = gc.open_by_key(SHEET_ID)
aba_userbase = sh.worksheet("Userbase")  # <-- CORRIGIDO: nome da aba

PASTA_TEMP = "/tmp/musica_temp"
os.makedirs(PASTA_TEMP, exist_ok=True)


# --------------------------------
# 3. Funções auxiliares — Drive genéricas
# --------------------------------

def nome_ficheiro_seguro(texto):
    """Remove caracteres inválidos para nomes de ficheiros (barras, dois pontos, etc)."""
    texto_normalizado = unicodedata.normalize("NFKD", texto)
    return re.sub(r'[\\/:*?"<>|]', "", texto_normalizado).strip()


def extrair_id_da_pasta(link_drive):
    """Extrai o ID da pasta a partir de um link do tipo .../folders/ESTE_ID."""
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", link_drive)
    if match:
        return match.group(1)
    raise ValueError("Link de pasta inválido: " + link_drive)


def encontrar_ou_criar_subpasta(nome_pasta, id_pasta_pai):
    """Procura (ou cria) uma subpasta com este nome dentro da pasta pai."""
    query = (
        f"name = '{nome_pasta}' and '{id_pasta_pai}' in parents "
        f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    resultado = drive_service.files().list(
        q=query, fields="files(id, name)",
        supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute()
    encontradas = resultado.get("files", [])
    if encontradas:
        return encontradas[0]["id"]

    metadata = {
        "name": nome_pasta,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [id_pasta_pai],
    }
    nova = drive_service.files().create(body=metadata, fields="id", supportsAllDrives=True).execute()
    return nova["id"]


def procurar_ficheiro_na_pasta(nome_ficheiro, id_pasta):
    """
    Procura um ficheiro pelo nome dentro de uma pasta específica.
    Devolve o ID do ficheiro se existir, ou None se não existir.
    """
    query = f"name = '{nome_ficheiro}' and '{id_pasta}' in parents and trashed = false"
    resultado = drive_service.files().list(
        q=query, fields="files(id)",
        supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute()
    encontrados = resultado.get("files", [])
    return encontrados[0]["id"] if encontrados else None


def upload_ficheiro_para_drive(caminho_local, nome_no_drive, id_pasta_destino, mime_type):
    """Faz upload (ou atualiza, se já existir) um ficheiro numa pasta do Drive."""
    file_id_existente = procurar_ficheiro_na_pasta(nome_no_drive, id_pasta_destino)
    media = MediaFileUpload(caminho_local, mimetype=mime_type, resumable=True)

    if file_id_existente:
        drive_service.files().update(
            fileId=file_id_existente, media_body=media, supportsAllDrives=True
        ).execute()
    else:
        metadata = {"name": nome_no_drive, "parents": [id_pasta_destino]}
        drive_service.files().create(
            body=metadata, media_body=media, fields="id", supportsAllDrives=True
        ).execute()


# --------------------------------
# 4. NOVO — Ler e gravar JSON de tracking dentro de UserData (em vez da Sheet)
# --------------------------------

def ler_json_do_drive(nome_ficheiro, id_pasta, valor_default):
    """
    Descarrega um ficheiro JSON de uma pasta do Drive e devolve-o já convertido
    em dicionário/lista Python. Se o ficheiro ainda não existir (primeira vez
    que este utilizador sincroniza), devolve o valor_default (normalmente {}).
    """
    file_id = procurar_ficheiro_na_pasta(nome_ficheiro, id_pasta)
    if not file_id:
        return valor_default

    # Descarrega o conteúdo do ficheiro para memória (sem gravar em disco)
    pedido = drive_service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, pedido)
    concluido = False
    while not concluido:
        _, concluido = downloader.next_chunk()

    buffer.seek(0)
    try:
        return json.loads(buffer.read().decode("utf-8"))
    except json.JSONDecodeError:
        # Ficheiro corrompido ou vazio -> começa do zero em vez de rebentar
        print(f"⚠️ '{nome_ficheiro}' não é um JSON válido, a começar do zero.")
        return valor_default


def guardar_json_no_drive(dados, nome_ficheiro, id_pasta):
    """
    Grava um dicionário/lista Python como ficheiro JSON dentro de uma pasta do Drive
    (cria um ficheiro local temporário e faz upload/atualização).
    """
    caminho_local = os.path.join(PASTA_TEMP, nome_ficheiro)
    with open(caminho_local, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)

    upload_ficheiro_para_drive(caminho_local, nome_ficheiro, id_pasta, "application/json")
    os.remove(caminho_local)


def obter_ids_ja_processados(registo_processadas, nome_playlist):
    """
    Recebe o dicionário completo já carregado (todas as playlists) e devolve
    o conjunto de youtube_ids já feitos APENAS para a playlist atual.
    """
    lista_da_playlist = registo_processadas.get(nome_playlist, [])
    return {item["youtube_id"] for item in lista_da_playlist}


def registar_musica_processada(registo_processadas, nome_playlist, youtube_id, titulo):
    """
    Adiciona uma música ao dicionário em memória (ainda não grava no Drive —
    isso só acontece uma vez no fim, para não fazer upload a cada música).
    """
    if nome_playlist not in registo_processadas:
        registo_processadas[nome_playlist] = []

    registo_processadas[nome_playlist].append({
        "youtube_id": youtube_id,
        "titulo": titulo,
        "data": time.strftime("%Y-%m-%d %H:%M:%S")
    })


# --------------------------------
# 5. LRCLIB e processamento de cada música (sem alterações)
# --------------------------------

def procurar_letra_lrclib(titulo, artista, duracao_segundos):
    """Consulta a API pública do LRCLIB à procura de letras sincronizadas (.lrc)."""
    try:
        resposta = requests.get(
            "https://lrclib.net/api/get",
            params={"track_name": titulo, "artist_name": artista, "duration": duracao_segundos},
            timeout=15,
        )
        if resposta.status_code == 200:
            return resposta.json().get("syncedLyrics")
    except Exception as erro:
        print("Aviso: LRCLIB falhou:", erro)
    return None


def processar_uma_musica(video_info, id_pasta_playlist, nome_playlist, registo_processadas):
    """
    Descarrega UMA música:
    1. MP3 com thumbnail embutida (capa)
    2. Link do YouTube guardado nos metadados (tag de comentário ID3) — ID único
    3. Letra sincronizada (.lrc) via LRCLIB
    4. Upload de ambos para a pasta da playlist no Drive
    5. Regista no dicionário em memória (gravado no Drive no fim de tudo)
    """
    youtube_id = video_info["id"]
    url_video = f"https://www.youtube.com/watch?v={youtube_id}"
    titulo_original = video_info.get("title", youtube_id)
    titulo_seguro = nome_ficheiro_seguro(titulo_original)
    caminho_base = os.path.join(PASTA_TEMP, titulo_seguro)

    opcoes_ytdlp = {
        "format": "bestaudio/best",
        "outtmpl": caminho_base + ".%(ext)s",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "EmbedThumbnail"},
            {"key": "FFmpegMetadata"},
        ],
        "writethumbnail": True,
        "quiet": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opcoes_ytdlp) as ydl:
        ydl.download([url_video])

    caminho_mp3 = caminho_base + ".mp3"
    if not os.path.exists(caminho_mp3):
        print(f"⚠️ Falhou o download de: {titulo_original}")
        return

    audio_tags = MP3(caminho_mp3, ID3=ID3)
    if audio_tags.tags is None:
        audio_tags.add_tags()
    audio_tags.tags.add(COMM(encoding=3, lang="eng", desc="youtube_url", text=url_video))
    audio_tags.save()

    duracao_segundos = int(video_info.get("duration", 0) or 0)
    letra_lrc = procurar_letra_lrclib(titulo_original, "", duracao_segundos)

    upload_ficheiro_para_drive(caminho_mp3, titulo_seguro + ".mp3", id_pasta_playlist, "audio/mpeg")

    if letra_lrc:
        caminho_lrc = caminho_base + ".lrc"
        with open(caminho_lrc, "w", encoding="utf-8") as f:
            f.write(letra_lrc)
        upload_ficheiro_para_drive(caminho_lrc, titulo_seguro + ".lrc", id_pasta_playlist, "text/plain")
    else:
        print(f"ℹ️ Sem letra sincronizada para: {titulo_original}")

    for ext in [".mp3", ".lrc", ".jpg", ".webp", ".png"]:
        caminho_temp = caminho_base + ext
        if os.path.exists(caminho_temp):
            os.remove(caminho_temp)

    # Regista em memória — a gravação real no Drive acontece uma vez no fim (main())
    registar_musica_processada(registo_processadas, nome_playlist, youtube_id, titulo_original)
    print(f"✅ Concluído: {titulo_original}")


# --------------------------------
# 6. Fluxo principal
# --------------------------------

def main():
    # 6.1 Buscar o link da pasta raiz do utilizador na aba "Userbase" (coluna C = drive_folder_link)
    registos = aba_userbase.get_all_records()
    link_pasta_utilizador = None
    for r in registos:
        if str(r.get("utilizador", "")).strip() == UTILIZADOR_ATUAL:
            link_pasta_utilizador = r.get("drive_folder_link")
            break

    if not link_pasta_utilizador:
        raise ValueError(f"Utilizador '{UTILIZADOR_ATUAL}' não encontrado na aba Userbase.")

    id_pasta_raiz = extrair_id_da_pasta(link_pasta_utilizador)

    # 6.2 Garante que existem as subpastas MusicData e UserData
    id_musicdata = encontrar_ou_criar_subpasta("MusicData", id_pasta_raiz)
    id_userdata = encontrar_ou_criar_subpasta("UserData", id_pasta_raiz)

    # 6.3 Carrega o registo de músicas já processadas (fica dentro de UserData)
    NOME_FICHEIRO_TRACKING = "processed_tracks.json"
    registo_processadas = ler_json_do_drive(NOME_FICHEIRO_TRACKING, id_userdata, valor_default={})

    # 6.4 Ler informação da playlist do YouTube (sem descarregar ainda)
    opcoes_lista = {"quiet": True, "extract_flat": "in_playlist"}
    with yt_dlp.YoutubeDL(opcoes_lista) as ydl:
        info_playlist = ydl.extract_info(PLAYLIST_URL, download=False)

    nome_playlist = nome_ficheiro_seguro(info_playlist.get("title", "Playlist"))
    videos = info_playlist.get("entries", [])
    print(f"Playlist: {nome_playlist} | {len(videos)} música(s) encontradas")

    id_pasta_playlist = encontrar_ou_criar_subpasta(nome_playlist, id_musicdata)

    # 6.5 EXTRA: capa da playlist
    thumbs = info_playlist.get("thumbnails", [])
    if thumbs:
        url_thumb = thumbs[-1]["url"]
        caminho_capa = os.path.join(PASTA_TEMP, "cover_playlist.jpg")
        resposta_img = requests.get(url_thumb, timeout=15)
        with open(caminho_capa, "wb") as f:
            f.write(resposta_img.content)
        upload_ficheiro_para_drive(caminho_capa, "cover.jpg", id_pasta_playlist, "image/jpeg")
        os.remove(caminho_capa)
        print("🖼️ Capa da playlist enviada.")

    # 6.6 Processar cada música que ainda falta
    ids_feitos = obter_ids_ja_processados(registo_processadas, nome_playlist)
    houve_alteracoes = False

    for video in videos:
        yid = video.get("id")
        if not yid:
            continue
        if yid in ids_feitos:
            print(f"⏭️ Já processado, a saltar: {video.get('title')}")
            continue
        try:
            processar_uma_musica(video, id_pasta_playlist, nome_playlist, registo_processadas)
            houve_alteracoes = True
        except Exception as erro:
            print(f"❌ Erro em '{video.get('title')}': {erro}")

    # 6.7 Gravar o registo atualizado de volta no Drive (só se algo mudou, poupa uma chamada)
    if houve_alteracoes:
        guardar_json_no_drive(registo_processadas, NOME_FICHEIRO_TRACKING, id_userdata)
        print("💾 Registo de músicas processadas atualizado no Drive.")

    print("🎉 Concluído.")


if __name__ == "__main__":
    main()
