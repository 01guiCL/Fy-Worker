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
from googleapiclient.http import MediaFileUpload
import gspread

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
aba_utilizadores = sh.worksheet("Utilizadores")
aba_processadas = sh.worksheet("Musicas_Processadas")

PASTA_TEMP = "/tmp/musica_temp"
os.makedirs(PASTA_TEMP, exist_ok=True)


# --------------------------------
# 3. Funções auxiliares
# --------------------------------

def nome_ficheiro_seguro(texto):
    """Remove caracteres inválidos para nomes de ficheiros (barras, dois pontos, etc)."""
    texto_normalizado = unicodedata.normalize("NFKD", texto)
    return re.sub(r'[\\/:*?"<>|]', "", texto_normalizado).strip()


def extrair_id_da_pasta(link_drive):
    """
    Extrai o ID da pasta a partir de um link do tipo:
    https://drive.google.com/drive/folders/ESTE_ID_AQUI?usp=sharing
    """
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", link_drive)
    if match:
        return match.group(1)
    raise ValueError("Link de pasta inválido: " + link_drive)


def encontrar_ou_criar_subpasta(nome_pasta, id_pasta_pai):
    """
    Procura uma subpasta com este nome dentro da pasta pai.
    Se não existir, cria-a. Evita duplicar pastas em execuções repetidas
    (importante para poder "retomar" um processamento interrompido).
    """
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


def upload_ficheiro_para_drive(caminho_local, nome_no_drive, id_pasta_destino, mime_type):
    """Faz upload (ou atualiza, se já existir) um ficheiro numa pasta do Drive."""
    query = f"name = '{nome_no_drive}' and '{id_pasta_destino}' in parents and trashed = false"
    resultado = drive_service.files().list(
        q=query, fields="files(id)",
        supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute()
    existentes = resultado.get("files", [])

    media = MediaFileUpload(caminho_local, mimetype=mime_type, resumable=True)

    if existentes:
        drive_service.files().update(
            fileId=existentes[0]["id"], media_body=media, supportsAllDrives=True
        ).execute()
    else:
        metadata = {"name": nome_no_drive, "parents": [id_pasta_destino]}
        drive_service.files().create(
            body=metadata, media_body=media, fields="id", supportsAllDrives=True
        ).execute()


def obter_ids_ja_processados(utilizador, nome_playlist):
    """Evita repetir músicas já descarregadas anteriormente (permite retomar sem duplicar)."""
    registos = aba_processadas.get_all_records()
    return {
        str(r.get("youtube_id"))
        for r in registos
        if str(r.get("utilizador")) == utilizador and str(r.get("playlist")) == nome_playlist
    }


def registar_musica_processada(utilizador, youtube_id, titulo, nome_playlist):
    """Regista na aba 'Musicas_Processadas' que esta música já foi tratada."""
    aba_processadas.append_row(
        [utilizador, youtube_id, titulo, nome_playlist, time.strftime("%Y-%m-%d %H:%M:%S")]
    )


def processar_uma_musica(video_info, id_pasta_playlist, utilizador, nome_playlist):
    """
    Descarrega UMA música:
    1. MP3 com thumbnail embutida (capa)
    2. Link do YouTube guardado nos metadados (tag de comentário ID3) — serve de ID único
    3. Letra sincronizada (.lrc) via LRCLIB
    4. Upload de ambos para a pasta da playlist no Drive
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
            {"key": "EmbedThumbnail"},   # embute a thumbnail como capa do MP3
            {"key": "FFmpegMetadata"},   # escreve metadados básicos (título, etc)
        ],
        "writethumbnail": True,
        "quiet": True,
        "noplaylist": True,  # download por vídeo individual, não pela playlist inteira
    }
    with yt_dlp.YoutubeDL(opcoes_ytdlp) as ydl:
        ydl.download([url_video])

    caminho_mp3 = caminho_base + ".mp3"
    if not os.path.exists(caminho_mp3):
        print(f"⚠️ Falhou o download de: {titulo_original}")
        return

    # Guardar o link do YouTube nos metadados (campo "comentário" do ID3 = ID único)
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

    # Limpar ficheiros locais temporários (poupar espaço na máquina do GitHub Actions)
    for ext in [".mp3", ".lrc", ".jpg", ".webp", ".png"]:
        caminho_temp = caminho_base + ext
        if os.path.exists(caminho_temp):
            os.remove(caminho_temp)

    # Regista como concluído (permite retomar sem repetir, se o processo for interrompido)
    registar_musica_processada(utilizador, youtube_id, titulo_original, nome_playlist)
    print(f"✅ Concluído: {titulo_original}")


# --------------------------------
# 4. Fluxo principal
# --------------------------------

def main():
    # 4.1 Buscar o link da pasta raiz do utilizador na aba "Utilizadores" (coluna C = drive_folder_link)
    registos = aba_utilizadores.get_all_records()
    link_pasta_utilizador = None
    for r in registos:
        if str(r.get("utilizador", "")).strip() == UTILIZADOR_ATUAL:
            link_pasta_utilizador = r.get("drive_folder_link")
            break

    if not link_pasta_utilizador:
        raise ValueError(f"Utilizador '{UTILIZADOR_ATUAL}' não encontrado na Sheet.")

    id_pasta_raiz = extrair_id_da_pasta(link_pasta_utilizador)

    # 4.2 Garante que existem as subpastas MusicData e UserData
    id_musicdata = encontrar_ou_criar_subpasta("MusicData", id_pasta_raiz)
    encontrar_ou_criar_subpasta("UserData", id_pasta_raiz)  # reservada para histórico/preferências

    # 4.3 Ler informação da playlist do YouTube (sem descarregar ainda)
    opcoes_lista = {"quiet": True, "extract_flat": "in_playlist"}
    with yt_dlp.YoutubeDL(opcoes_lista) as ydl:
        info_playlist = ydl.extract_info(PLAYLIST_URL, download=False)

    nome_playlist = nome_ficheiro_seguro(info_playlist.get("title", "Playlist"))
    videos = info_playlist.get("entries", [])
    print(f"Playlist: {nome_playlist} | {len(videos)} música(s) encontradas")

    id_pasta_playlist = encontrar_ou_criar_subpasta(nome_playlist, id_musicdata)

    # 4.4 EXTRA: descarregar e enviar a capa da playlist
    thumbs = info_playlist.get("thumbnails", [])
    if thumbs:
        url_thumb = thumbs[-1]["url"]  # a última é normalmente a de maior resolução
        caminho_capa = os.path.join(PASTA_TEMP, "cover_playlist.jpg")
        resposta_img = requests.get(url_thumb, timeout=15)
        with open(caminho_capa, "wb") as f:
            f.write(resposta_img.content)
        upload_ficheiro_para_drive(caminho_capa, "cover.jpg", id_pasta_playlist, "image/jpeg")
        os.remove(caminho_capa)
        print("🖼️ Capa da playlist enviada.")

    # 4.5 Processar cada música que ainda falta (salta as já feitas)
    ids_feitos = obter_ids_ja_processados(UTILIZADOR_ATUAL, nome_playlist)
    for video in videos:
        yid = video.get("id")
        if not yid:
            continue
        if yid in ids_feitos:
            print(f"⏭️ Já processado, a saltar: {video.get('title')}")
            continue
        try:
            processar_uma_musica(video, id_pasta_playlist, UTILIZADOR_ATUAL, nome_playlist)
        except Exception as erro:
            # Uma música falhar não deve parar as restantes
            print(f"❌ Erro em '{video.get('title')}': {erro}")

    print("🎉 Concluído.")


if __name__ == "__main__":
    main()
