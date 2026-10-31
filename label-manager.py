"""
Gmail: lista los labels en uso y exporta los correos que no tienen ningún label de usuario.

Requisitos:
    pip install google-api-python-client google-auth-oauthlib

Setup (una sola vez):
    1. En Google Cloud Console, habilita la "Gmail API" en un proyecto.
    2. Crea credenciales OAuth tipo "Desktop app" y descarga el JSON como credentials.json
       en la misma carpeta que este script.
    3. La primera ejecución abre el navegador para autorizar; se guarda token.json.

Uso:
    python gmail_labels.py                 # labels + todos los correos sin label
    python gmail_labels.py --inbox-only    # solo correos sin label que están en Inbox
    python gmail_labels.py --limit 500     # tope de correos a exportar
    python label-manager.py --labels-only  # solo el resumen de labels
    python label-manager.py --check-only   # solo comprobar si existen correos sin label
    python label-manager.py --apply-label   # aplicar el label a esos correos
"""

import argparse
import csv
import json
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_FILE = "token.json"
CREDS_FILE = "credentials.json"
OUTPUT_CSV = "correos_sin_label.csv"
TARGET_LABEL_NAME = "To Check/Not User Labels"
BATCH_SIZE = 50  # Gmail recomienda lotes chicos para no chocar con el rate limit


def get_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        with open(TOKEN_FILE, encoding="utf-8") as token_file:
            token_data = json.load(token_file)
        granted_scopes = set((token_data.get("scope") or "").split())
        # Un token anterior con solo gmail.readonly no sirve para aplicar labels.
        if not granted_scopes or not set(SCOPES).issubset(granted_scopes):
            creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDS_FILE):
                sys.exit(f"Falta {CREDS_FILE}. Revisa el setup al inicio del script.")
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def list_labels(service):
    """Devuelve los labels con su conteo de mensajes, separados en usuario / sistema."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    detailed = []
    for lbl in labels:
        info = service.users().labels().get(userId="me", id=lbl["id"]).execute()
        detailed.append({
            "id": info["id"],
            "name": info["name"],
            "type": info["type"],  # "user" o "system"
            "messages": info.get("messagesTotal", 0),
            "unread": info.get("messagesUnread", 0),
        })
    detailed.sort(key=lambda x: (x["type"] != "user", -x["messages"]))
    return detailed


def print_labels(labels):
    user = [l for l in labels if l["type"] == "user"]
    system = [l for l in labels if l["type"] == "system"]

    print(f"\n=== Labels de usuario ({len(user)}) ===")
    print(f"{'Label':45} {'Correos':>8} {'No leídos':>10}")
    for l in user:
        flag = "  (vacío)" if l["messages"] == 0 else ""
        print(f"{l['name'][:45]:45} {l['messages']:>8} {l['unread']:>10}{flag}")

    print(f"\n=== Labels de sistema ({len(system)}) ===")
    for l in system:
        print(f"{l['name'][:45]:45} {l['messages']:>8} {l['unread']:>10}")


def get_or_create_label(service, name):
    """Devuelve el ID de un label de usuario, creándolo si hace falta."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    existing = next((label for label in labels if label["name"] == name), None)
    if existing:
        return existing["id"]

    created = service.users().labels().create(
        userId="me",
        body={
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    print(f"Label creado: {name}")
    return created["id"]


def apply_label(service, ids, label_id):
    """Añade el label a los mensajes en lotes."""
    for i in range(0, len(ids), 1000):
        batch_ids = ids[i:i + 1000]
        service.users().messages().batchModify(
            userId="me",
            body={"ids": batch_ids, "addLabelIds": [label_id]},
        ).execute()
        print(f"  Labels aplicados: {min(i + 1000, len(ids))}/{len(ids)}")


def list_message_ids(service, query, limit=None):
    ids, page_token = [], None
    while True:
        resp = service.users().messages().list(
            userId="me", q=query, pageToken=page_token, maxResults=500
        ).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token or (limit and len(ids) >= limit):
            break
    return ids[:limit] if limit else ids


def fetch_metadata(service, ids):
    """Trae From/Subject/Date en lotes para no hacer una llamada por correo."""
    rows = []

    def callback(_req_id, response, exception):
        if exception:
            print(f"  ! error: {exception}", file=sys.stderr)
            return
        headers = {h["name"]: h["value"] for h in response["payload"].get("headers", [])}
        rows.append({
            "id": response["id"],
            "fecha": headers.get("Date", ""),
            "de": headers.get("From", ""),
            "asunto": headers.get("Subject", ""),
            "labels_sistema": ",".join(response.get("labelIds", [])),
            "link": f"https://mail.google.com/mail/u/0/#all/{response['id']}",
        })

    for i in range(0, len(ids), BATCH_SIZE):
        batch = service.new_batch_http_request(callback=callback)
        for mid in ids[i:i + BATCH_SIZE]:
            batch.add(service.users().messages().get(
                userId="me", id=mid, format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ))
        batch.execute()
        print(f"  {min(i + BATCH_SIZE, len(ids))}/{len(ids)}", end="\r")
    print()
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inbox-only", action="store_true", help="solo correos en Inbox")
    parser.add_argument("--limit", type=int, default=None, help="máximo de correos a exportar")
    parser.add_argument("--labels-only", action="store_true", help="solo listar labels")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="solo comprobar si existen correos sin labels de usuario",
    )
    parser.add_argument(
        "--apply-label",
        action="store_true",
        help=f"aplicar el label {TARGET_LABEL_NAME} a los correos encontrados",
    )
    args = parser.parse_args()

    service = get_service()

    labels = list_labels(service)
    print_labels(labels)
    if args.labels_only:
        return

    # has:nouserlabels = sin labels creados por ti (ignora INBOX, UNREAD, CATEGORY_*, etc.)
    # Spam y Papelera quedan fuera por defecto en la API.
    query = "has:nouserlabels -in:chats"
    if args.inbox_only:
        query += " in:inbox"

    print(f"\nBuscando correos con: {query}")
    ids = list_message_ids(service, query, args.limit)
    print(f"Encontrados: {len(ids)}")
    if ids:
        print(f"Sí: hay {len(ids)} correo(s) sin labels de usuario.")
    else:
        print("No: no hay correos sin labels de usuario.")

    if args.apply_label:
        label_id = get_or_create_label(service, TARGET_LABEL_NAME)
        apply_label(service, ids, label_id)
        print(f"Listo: {len(ids)} correo(s) recibieron el label {TARGET_LABEL_NAME}.")

    if not ids or args.check_only or args.apply_label:
        return

    rows = fetch_metadata(service, ids)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Exportado a {OUTPUT_CSV} ({len(rows)} correos)")


if __name__ == "__main__":
    main()