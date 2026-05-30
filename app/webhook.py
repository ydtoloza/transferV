from __future__ import annotations

import json
from string import Template

import httpx

from app.models import AppSettings, TransferRecord, TransferStatus


def render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return Template(rendered).safe_substitute(values)


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    elif size < 1024 ** 2:
        return f"{size / 1024:.2f} KB"
    elif size < 1024 ** 3:
        return f"{size / 1024 ** 2:.2f} MB"
    else:
        return f"{size / 1024 ** 3:.2f} GB"


async def send_webhook(
    settings: AppSettings,
    transfer: TransferRecord,
    status: TransferStatus,
    message: str,
) -> None:
    webhook = settings.webhook
    if not webhook.enabled or not webhook.url:
        return

    values = {
        "status": status.value,
        "torrent_name": transfer.torrent_name,
        "torrent_hash": transfer.torrent_hash,
        "source_path": transfer.source_path,
        "destination_path": transfer.destination_path,
        "size": str(transfer.size),
        "size_human": format_bytes(transfer.size),
        "message": message,
        "created_at": transfer.created_at,
        "completed_at": transfer.completed_at or "",
    }
    headers = json.loads(webhook.headers_json or "{}")
    
    def replace_in_obj(obj):
        if isinstance(obj, dict):
            return {k: replace_in_obj(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [replace_in_obj(v) for v in obj]
        elif isinstance(obj, str):
            return render_template(obj, values)
        return obj

    try:
        # Si el template es un JSON válido, reemplazamos en los valores cacheados
        # para que json=body serialice correctamente caracteres especiales y saltos de línea.
        template_obj = json.loads(webhook.body_template)
        body = replace_in_obj(template_obj)
    except json.JSONDecodeError:
        # Fallback a reemplazo de texto crudo
        body_text = render_template(webhook.body_template, values)
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError:
            body = body_text

    kwargs = {"headers": headers}
    if webhook.method.upper() in ("POST", "PUT", "PATCH"):
        if isinstance(body, dict):
            kwargs["json"] = body
        else:
            kwargs["data"] = body
    
    async with httpx.AsyncClient(timeout=20) as client:
        await client.request(webhook.method.upper(), webhook.url, **kwargs)

