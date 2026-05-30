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
        "message": message,
    }
    headers = json.loads(webhook.headers_json or "{}")
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

