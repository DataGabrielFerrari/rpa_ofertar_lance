import re
import unicodedata


def remover_acentos(texto: str) -> str:
    texto = str(texto or "")
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", texto)
        if not unicodedata.combining(ch)
    )


def normalizar(texto: str) -> str:
    texto = str(texto or "").strip().upper()
    texto = remover_acentos(texto)
    texto = texto.replace("_", " ")
    texto = texto.replace("-", " ")
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()