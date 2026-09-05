# ================================
# worker.py — versão OAuth por utilizador (sem Service Account, sem Sheet)
# Recebe: drive_folder_link, playlist_url, google_access_token
# ================================

import sys
import os
import re
import json
import time
import unicodedata
import requests
import yt_dlp
import io
from mutagen.id3 import ID3, COMM
from mutagen.mp3 import MP3
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# --------------------------------
# 1. Argumentos recebidos
# --------------------------------
DRIVE_FOLDER_LINK = sys.argv[1]
PLAYLIST_URL = sys.argv[2]
GOOGLE_ACCESS_TOKEN = sys.argv[3]

# --------------------------------
# 2. Autenticação — usa o access_token do PRÓPRIO utilizador
# (gerado pelo Apps Script a partir do refresh_token dele)
# --------------------------------
creds = Credentials(token=GOOGLE_ACCESS_TOKEN)
drive_service = build("drive", "v3", credentials=creds)

PASTA_TEMP = "/tmp/musica_temp"
os.makedirs(PASTA_TEMP, exist_ok=True)


# --------------------------------
# 3. Funções auxiliares (Drive)
# --------------------------------

def nome_ficheiro_seguro(texto):
    texto_normalizado = unicodedata.normalize("NFKD", texto)
    return re.sub(r'[\\/:*?"<>|]', "", texto_normalizado).strip()


def extrair_id_da_pasta(link_drive):
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", link_drive)
    if match:
        return match.group(1)
    raise ValueError("Link de pasta inválido: " + link_drive)


def encontrar_ou_criar_subpasta(nome_pasta, id_pasta_pai):
    query = (
        f"name = '{nome_pasta}' and '{id_pasta_pai}' in parents "
        f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    resultado = drive_service.files().list(q=query, fields="files(id, name)").execute()
    encontradas = resultado.get("files", [])
    if encontradas:
        return encontradas[0]["id"]

    metadata = {"name": nome_pasta, "mimeType": "application/vnd.google-apps.folder", "parents": [id_pasta_pai]}
    nova = drive_service.files().create(body=metadata, fields="id").execute()
    return nova["id"]


def procurar_ficheiro_na_pasta(nome_ficheiro, id_pasta):
    query = f"name = '{nome_ficheiro}' and '{id_pasta}' in parents and trashed = false"
    resultado = drive_service.files().list(q=query, fields="files(id)").execute()
    encontrados = resultado.get("files", [])
    return encontrados[0]["id"] if encontrados else None


def upload_ficheiro_para_drive(caminho_local, nome_no_drive, id_pasta_destino, mime_type):
    file_id_existente = procurar_ficheiro_na_pasta(nome_no_drive, id_pasta_destino)
    media = MediaFileUpload(caminho_local, mimetype=mime_type, resumable=True)

    if file_id_existente:
        drive_service.files().update(fileId=file_id_existente, media_body=media).execute()
    else:
        metadata = {"name": nome_no_drive, "parents": [id_pasta_destino]}
        drive_service.files().create(body=metadata, media_body=media, fields="id").execute()


def ler_json_do_drive(nome_ficheiro, id_pasta, valor_default):
    file_id = procurar_ficheiro_na_pasta(nome_ficheiro, id_pasta)
    if not file_id:
        return valor_default

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
        return valor_default


def guardar_json_no_drive(dados, nome_ficheiro, id_pasta):
    caminho_local = os.path.join(PASTA_TEMP, nome_ficheiro)
    with open(caminho_local, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    upload_ficheiro_para_drive(caminho_local, nome_ficheiro, id_pasta, "application/json")
    os.remove(caminho_local)


def obter_ids_ja_processados(registo_processadas, nome_playlist):
    return {item["youtube_id"] for item in registo_processadas.get(nome_playlist, [])}


def registar_musica_processada(registo_processadas, nome_playlist, youtube_id, titulo):
    if nome_playlist not in registo_processadas:
        registo_processadas[nome_playlist] = []
    registo_processadas[nome_playlist].append({
        "youtube_id": youtube_id, "titulo": titulo, "data": time.strftime("%Y-%m-%d %H:%M:%S")
    })


def procurar_letra_lrclib(titulo, artista, duracao_segundos):
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
        "cookiefile": "cookies.txt",
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

    for ext in [".mp3", ".lrc", ".jpg", ".webp", ".png"]:
        caminho_temp = caminho_base + ext
        if os.path.exists(caminho_temp):
            os.remove(caminho_temp)

    registar_musica_processada(registo_processadas, nome_playlist, youtube_id, titulo_original)
    print(f"✅ Concluído: {titulo_original}")


def main():
    id_pasta_raiz = extrair_id_da_pasta(DRIVE_FOLDER_LINK)
    id_musicdata = encontrar_ou_criar_subpasta("MusicData", id_pasta_raiz)
    id_userdata = encontrar_ou_criar_subpasta("UserData", id_pasta_raiz)

    NOME_FICHEIRO_TRACKING = "processed_tracks.json"
    registo_processadas = ler_json_do_drive(NOME_FICHEIRO_TRACKING, id_userdata, valor_default={})

    opcoes_lista = {"quiet": True, "extract_flat": "in_playlist", "cookiefile": "cookies.txt"}
    with yt_dlp.YoutubeDL(opcoes_lista) as ydl:
        info_playlist = ydl.extract_info(PLAYLIST_URL, download=False)

    nome_playlist = nome_ficheiro_seguro(info_playlist.get("title", "Playlist"))
    videos = info_playlist.get("entries", [])
    print(f"Playlist: {nome_playlist} | {len(videos)} música(s) encontradas")

    id_pasta_playlist = encontrar_ou_criar_subpasta(nome_playlist, id_musicdata)

    thumbs = info_playlist.get("thumbnails", [])
    if thumbs:
        url_thumb = thumbs[-1]["url"]
        caminho_capa = os.path.join(PASTA_TEMP, "cover_playlist.jpg")
        resposta_img = requests.get(url_thumb, timeout=15)
        with open(caminho_capa, "wb") as f:
            f.write(resposta_img.content)
        upload_ficheiro_para_drive(caminho_capa, "cover.jpg", id_pasta_playlist, "image/jpeg")
        os.remove(caminho_capa)

    ids_feitos = obter_ids_ja_processados(registo_processadas, nome_playlist)
    houve_alteracoes = False

    for video in videos:
        yid = video.get("id")
        if not yid or yid in ids_feitos:
            continue
        try:
            processar_uma_musica(video, id_pasta_playlist, nome_playlist, registo_processadas)
            houve_alteracoes = True
        except Exception as erro:
            print(f"❌ Erro em '{video.get('title')}': {erro}")

    if houve_alteracoes:
        guardar_json_no_drive(registo_processadas, NOME_FICHEIRO_TRACKING, id_userdata)

    print("🎉 Concluído.")


if __name__ == "__main__":
    main()
