import base64
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from credenciais.google_auth import criar_servico_gmail


EMAIL_DESTINO = "rpa.ademicon@gmail.com"


def _montar_corpo_txt(status: str, observacao: str, data_hora: str) -> str:
    return f"""
Alerta de Falha – RPA Ofertar Lance

Status    : {status}
Observação: {observacao}
Data/Hora : {data_hora}

Este e-mail foi gerado automaticamente pelo sistema RPA.
""".strip()


def _montar_corpo_html(status: str, observacao: str, data_hora: str) -> str:
    cor_status = "#d93025" if status.upper() == "FALHA" else "#d97706"

    return f"""
<html>
  <body style="font-family: Arial, sans-serif; font-size:14px; color:#333;">
    <div style="max-width:720px; margin:0 auto;">
      <h2 style="color:#d93025;">⚠️ Alerta de Falha – RPA Ofertar Lance</h2>

      <table style="border-collapse:collapse; width:100%; max-width:520px;">
        <tr>
          <td style="border:1px solid #e5e7eb; padding:8px; width:140px;"><strong>Status</strong></td>
          <td style="border:1px solid #e5e7eb; padding:8px; color:{cor_status};">
            <strong>{status}</strong>
          </td>
        </tr>
        <tr>
          <td style="border:1px solid #e5e7eb; padding:8px;"><strong>Observação</strong></td>
          <td style="border:1px solid #e5e7eb; padding:8px;">{observacao}</td>
        </tr>
        <tr>
          <td style="border:1px solid #e5e7eb; padding:8px;"><strong>Data/Hora</strong></td>
          <td style="border:1px solid #e5e7eb; padding:8px;">{data_hora}</td>
        </tr>
      </table>

      <p style="font-size:12px; color:#777; margin-top:30px;">
        Este e-mail foi gerado automaticamente pelo sistema RPA.
      </p>
    </div>
  </body>
</html>
""".strip()


def enviar_email_falha(status: str, observacao: str, logger) -> None:
    data_hora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    assunto = f"[RPA] Alerta de Falha – {status} | {data_hora}"

    corpo_txt = _montar_corpo_txt(status, observacao, data_hora)
    corpo_html = _montar_corpo_html(status, observacao, data_hora)

    service = criar_servico_gmail()

    msg = MIMEMultipart("alternative")
    msg["to"] = EMAIL_DESTINO
    msg["subject"] = assunto

    msg.attach(MIMEText(corpo_txt, "plain", "utf-8"))
    msg.attach(MIMEText(corpo_html, "html", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    service.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute()

    logger.info(f"[EMAIL FALHA] Enviado para {EMAIL_DESTINO} | status={status}")