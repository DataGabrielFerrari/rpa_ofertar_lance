import os
import re
import zipfile
from datetime import datetime

from googleapiclient.http import MediaFileUpload

from credenciais.google_auth import criar_servico_drive
from db.db import fetchone
from db.funcoes import atualizar_link_drive_fila_adm


def _nome_seguro_arquivo(texto: str, max_len: int = 80) -> str:
    texto = str(texto or "").strip()
    texto = re.sub(r'[<>:"/\\|?*\n\r\t]', "-", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    if len(texto) > max_len:
        texto = texto[:max_len].rstrip()
    return texto


def _obter_dados_drive_lote(id_fila_adm: int):
    # caminho_base é a raiz do lote: .../lotes/imovel/NomeAdm_1/fila_17/
    # a pasta de evidências fica dentro de caminho_base/evidencias/
    sql = """
        SELECT
            fa.caminho_base,
            a.nome
        FROM tbl_fila_adm fa
        INNER JOIN tbl_adm a ON a.id_adm = fa.id_adm
        WHERE fa.id_fila_adm = %s
    """
    row = fetchone(sql, (id_fila_adm,))
    if not row:
        raise ValueError(f"Lote não encontrado: id_fila_adm={id_fila_adm}")

    caminho_base = str(row[0] or "").strip()
    nome_adm     = str(row[1] or "").strip()

    if not caminho_base:
        raise ValueError(f"caminho_base vazio para id_fila_adm={id_fila_adm}")

    caminho_evidencias = os.path.join(caminho_base, "evidencias")

    if not os.path.isdir(caminho_evidencias):
        raise FileNotFoundError(
            f"Pasta de evidências não encontrada: {caminho_evidencias}"
        )

    return caminho_evidencias, nome_adm


def zipar_pasta_evidencias(caminho_evidencias: str, nome_zip: str) -> str:
    pasta_lote   = os.path.dirname(caminho_evidencias)
    caminho_zip  = os.path.join(pasta_lote, nome_zip)

    with zipfile.ZipFile(caminho_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(caminho_evidencias):
            for file_name in files:
                full_path = os.path.join(root, file_name)
                rel_path  = os.path.relpath(full_path, caminho_evidencias)
                zf.write(full_path, arcname=rel_path)

    return caminho_zip


def subir_zip_drive_publico(caminho_zip: str, logger) -> str:
    service = criar_servico_drive()

    logger.info(f"[DRIVE] Upload iniciado | arquivo={caminho_zip}")

    media = MediaFileUpload(caminho_zip, mimetype="application/zip", resumable=True)

    arquivo = service.files().create(
        body={"name": os.path.basename(caminho_zip)},
        media_body=media,
        fields="id",
    ).execute()

    file_id = arquivo["id"]

    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
    logger.info(f"[DRIVE] Upload concluído | file_id={file_id}")

    return link


def processar_drive_lote(id_fila_adm: int, logger) -> str:
    caminho_evidencias, nome_adm = _obter_dados_drive_lote(id_fila_adm)

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    nome_zip = f"Evidencias_{_nome_seguro_arquivo(nome_adm)}_{id_fila_adm}_{ts}.zip"

    caminho_zip = zipar_pasta_evidencias(caminho_evidencias, nome_zip)
    logger.info(f"[DRIVE] ZIP gerado | caminho={caminho_zip}")

    link_drive = subir_zip_drive_publico(caminho_zip, logger)
    atualizar_link_drive_fila_adm(id_fila_adm, link_drive)

    logger.info(f"[DRIVE] Link salvo no banco | id_fila_adm={id_fila_adm}")
    return link_drive