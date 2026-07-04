import os

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.send",
]


def _obter_caminhos_credenciais():
    cred_dir = os.path.abspath(os.path.dirname(__file__))
    client_secret = os.path.join(cred_dir, "client_secret.json")
    token_path = os.path.join(cred_dir, "token.json")
    return cred_dir, client_secret, token_path


def obter_credenciais_google() -> Credentials:
    cred_dir, client_secret, token_path = _obter_caminhos_credenciais()

    if not os.path.exists(client_secret):
        raise FileNotFoundError(f"Não achei o arquivo: {client_secret}")

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                # Token revogado ou expirado sem possibilidade de refresh —
                # abre o navegador para reautenticacao e salva novo token.
                creds = None

        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


def criar_servico_sheets():
    return build("sheets", "v4", credentials=obter_credenciais_google())


def criar_servico_drive():
    return build("drive", "v3", credentials=obter_credenciais_google())


def criar_servico_gmail():
    return build("gmail", "v1", credentials=obter_credenciais_google())