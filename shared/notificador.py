"""
Notificador de falhas via Gmail API.

Reaproveita as credenciais OAuth do projeto (criar_servico_gmail) e envia
um email com:
  - contexto do erro (etapa, id_fila_adm, id_cota, máquina, usuário, hora)
  - traceback completo
  - anexo do log.txt do lote (quando caminho_log é informado)
  - anexo do script Python que falhou (quando script_path é informado)

Uso típico dentro de um except:

    from shared.notificador import notificar_falha

    try:
        ...
    except Exception as e:
        notificar_falha(
            etapa="PROCESSAMENTO",
            erro=e,
            id_fila_adm=id_fila_adm,
            caminho_log=caminho_log,
            script_path=__file__,
        )
        raise
"""

import os
import base64
import socket
import traceback as tb_module
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from typing import Optional, List, Iterable

from credenciais.google_auth import criar_servico_gmail


# Email destino padrao - pode ser sobrescrito via variavel de ambiente
EMAIL_DESTINO_PADRAO = os.getenv(
    "EMAIL_NOTIFICACAO_FALHA",
    "rpa.ademicon@gmail.com",
)


def _anexar_arquivo(msg: MIMEMultipart, caminho_arquivo: Optional[str]) -> None:
    """
    Anexa um arquivo ao email se o caminho existir e for legivel.
    Falhas no anexo nao devem derrubar o envio do email.
    """
    if not caminho_arquivo:
        return

    try:
        if not os.path.exists(caminho_arquivo):
            print(
                f"[NOTIFICADOR] Anexo nao encontrado: {caminho_arquivo}",
                flush=True,
            )
            return

        with open(caminho_arquivo, "rb") as f:
            conteudo = f.read()

        nome_arquivo = os.path.basename(caminho_arquivo)
        anexo = MIMEBase("application", "octet-stream")
        anexo.set_payload(conteudo)
        encoders.encode_base64(anexo)
        anexo.add_header(
            "Content-Disposition",
            f'attachment; filename="{nome_arquivo}"',
        )
        msg.attach(anexo)

    except Exception as e:
        print(
            f"[NOTIFICADOR] Falha ao anexar '{caminho_arquivo}': {e}",
            flush=True,
        )


def _formatar_corpo(
    etapa: str,
    erro: Exception,
    traceback_str: str,
    id_fila_adm: Optional[int],
    id_cota: Optional[int],
    caminho_log: Optional[str],
    script_path: Optional[str],
    contexto_extra: Optional[str],
) -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    maquina = os.environ.get("COMPUTERNAME") or socket.gethostname() or "DESCONHECIDA"
    usuario = os.environ.get("USERNAME") or "DESCONHECIDO"

    return f"""Falha detectada no RPA de Ofertar Lance (Ademicon).

==== CONTEXTO ====
Data/Hora    : {timestamp}
Etapa        : {etapa}
Maquina      : {maquina}
Usuario      : {usuario}
id_fila_adm  : {id_fila_adm if id_fila_adm is not None else '-'}
id_cota      : {id_cota if id_cota is not None else '-'}
Script       : {script_path or '-'}
Caminho log  : {caminho_log or '-'}

==== ERRO ====
Tipo     : {type(erro).__name__}
Mensagem : {erro}

==== TRACEBACK ====
{traceback_str}

==== CONTEXTO EXTRA ====
{contexto_extra or '(nenhum)'}

---
Email automatico gerado pelo notificador de falhas.
Anexos (quando disponiveis): log.txt do lote e script Python que falhou.
"""


def _gravar_fallback_arquivo(
    etapa: str,
    erro: Exception,
    traceback_str: str,
    id_fila_adm: Optional[int],
    id_cota: Optional[int],
    caminho_log: Optional[str],
    script_path: Optional[str],
    contexto_extra: Optional[str],
    motivo_fallback: str,
) -> None:
    """
    Quando o Gmail API falha (ex: token revogado), grava a notificacao
    em <ROOT>/Lotes/notificacoes_pendentes/<timestamp>.txt para que o
    operador veja na proxima execucao manual.
    """
    try:
        base_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..")
        )
        pasta = os.path.join(base_dir, "Lotes", "notificacoes_pendentes")
        os.makedirs(pasta, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        nome = f"{ts}__{etapa}.txt"
        caminho = os.path.join(pasta, nome)

        with open(caminho, "w", encoding="utf-8") as f:
            f.write(f"NOTIFICACAO PENDENTE (Gmail indisponivel)\n")
            f.write(f"Motivo do fallback: {motivo_fallback}\n")
            f.write("=" * 60 + "\n")
            f.write(_formatar_corpo(
                etapa=etapa,
                erro=erro,
                traceback_str=traceback_str,
                id_fila_adm=id_fila_adm,
                id_cota=id_cota,
                caminho_log=caminho_log,
                script_path=script_path,
                contexto_extra=contexto_extra,
            ))

        print(f"[NOTIFICADOR] Fallback gravado em: {caminho}", flush=True)
    except Exception as e_fb:
        print(f"[NOTIFICADOR] Fallback de arquivo TAMBEM falhou: {e_fb}", flush=True)


def notificar_falha(
    etapa: str,
    erro: Exception,
    id_fila_adm: Optional[int] = None,
    id_cota: Optional[int] = None,
    caminho_log: Optional[str] = None,
    script_path: Optional[str] = None,
    contexto_extra: Optional[str] = None,
    email_destino: Optional[str] = None,
    origem: str = "PYTHON",
    anexos_extras: Optional[Iterable[str]] = None,
) -> bool:
    """
    Envia email de notificacao de falha via Gmail API.

    `origem` controla o prefixo do assunto:
      - "PYTHON" (default) -> assunto "[PYTHON][...]" para erros vindos
        diretamente dos scripts Python
      - "PAD" -> assunto "[PAD][...]" para erros vindos do PAD via
        notificar_pad.py (PowerShell, UI Edge, F_Login, etc.)

    `anexos_extras` permite anexar arquivos adicionais alem do log e do
    script (por exemplo, screenshots de evidencia de falha de login).

    Retorna True se enviou com sucesso, False caso contrario.
    Nunca levanta excecao para nao mascarar o erro original.
    """
    destino = email_destino or EMAIL_DESTINO_PADRAO

    # Captura traceback ATUAL (funciona dentro de um except). Fora do try
    # principal para que o fallback tambem tenha acesso a ele.
    traceback_str = tb_module.format_exc()
    if not traceback_str or traceback_str.strip() == "NoneType: None":
        try:
            traceback_str = "".join(
                tb_module.format_exception(
                    type(erro),
                    erro,
                    erro.__traceback__,
                )
            )
        except Exception:
            traceback_str = f"(traceback indisponivel) {erro}"

    try:
        assunto = (
            f"[{origem}][{etapa}] Falha RPA Ofertar Lance | "
            f"id_fila_adm={id_fila_adm if id_fila_adm is not None else '-'}"
        )

        corpo = _formatar_corpo(
            etapa=etapa,
            erro=erro,
            traceback_str=traceback_str,
            id_fila_adm=id_fila_adm,
            id_cota=id_cota,
            caminho_log=caminho_log,
            script_path=script_path,
            contexto_extra=contexto_extra,
        )

        msg = MIMEMultipart()
        msg["to"] = destino
        msg["subject"] = assunto
        msg.attach(MIMEText(corpo, "plain", "utf-8"))

        # Anexos: log do lote + script que falhou + extras (ex.: screenshot)
        _anexar_arquivo(msg, caminho_log)
        _anexar_arquivo(msg, script_path)
        if anexos_extras:
            for caminho in anexos_extras:
                _anexar_arquivo(msg, caminho)

        service = criar_servico_gmail()
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()

        print(
            f"[NOTIFICADOR] Email de falha enviado para {destino} "
            f"(etapa={etapa}, id_fila_adm={id_fila_adm})",
            flush=True,
        )
        return True

    except Exception as e_notif:
        print(
            f"[NOTIFICADOR] FALHA AO ENVIAR EMAIL DE NOTIFICACAO: {e_notif}",
            flush=True,
        )
        try:
            tb_module.print_exc()
        except Exception:
            pass

        # Fallback: grava em arquivo para nao perder a notificacao.
        # Util quando o token Google esta revogado/expirado, sem internet, etc.
        _gravar_fallback_arquivo(
            etapa=etapa,
            erro=erro,
            traceback_str=traceback_str,
            id_fila_adm=id_fila_adm,
            id_cota=id_cota,
            caminho_log=caminho_log,
            script_path=script_path,
            contexto_extra=contexto_extra,
            motivo_fallback=str(e_notif),
        )
        return False
