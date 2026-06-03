# TransferV

TransferV is a small self-hosted web app for watching a qBittorrent instance and queueing completed downloads for transfer to a media server with `rsync` over SSH.

## ✨ Features

* **Multiple Deployment Modes**:
  * `local_pull`: TransferV runs on the destination server and pulls files from the VPS.
  * `orchestrated_pull`: TransferV runs on a third server, SSHs into the destination server, and tells it to pull files from the VPS.
  * `remote_push`: TransferV SSHs into the VPS and tells it to push to the media server.
* **Smart Queueing**: Keeps a local SQLite queue to manage transfers reliably.
* **Seeding Support**: Leaves torrents seeding in qBittorrent while transferring files.
* **Notifications**: Webhook support to notify you when transfers finish or fail (Discord, Slack, Telegram, WhatsApp, Ntfy).
* **Modern UI**: Clean and responsive web interface to monitor and manage your transfers.

## 🚀 Quick Start

```bash
docker compose up --build
```

Open your browser at:
```text
http://localhost:8080
```

*The default data directory is mounted at `./data`.*

## 📦 Production Deploy

GitHub Actions builds and publishes this image on every push to `main`:
`ghcr.io/ydtoloza/transferv:latest`

On the deploy VPS:
```bash
git clone git@github.com:ydtoloza/transferV.git
cd transferV
cp .env.example .env
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

For updates after the initial deploy, the app code comes from the image. Pull the image and recreate the container:
```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

If the compose file itself changes, run `git pull` first. Alternatively, use the update script:
```bash
sh deploy/update.sh
```

## ⚙️ First Configuration

1. **qBittorrent Setup**: Set the WebUI URL, username, password, and download path.
   * *Tip*: If qBittorrent runs in Docker, set the container path (e.g. `/downloads`) and the real SSH host path (e.g. `/home/mediaserver/media/downloads`).
2. **Destination Path**: Set the fixed destination path where completed downloads should land.
3. **Transfer Mode**: Choose your preferred transfer method (`local_pull`, `orchestrated_pull`, or `remote_push`).
4. **SSH Configuration**: Configure SSH for the VPS and, when needed, for the destination server.
5. **Webhooks**: Configure the webhook URL, headers, and body template for notifications.

## 🔔 Webhooks & Notificaciones

Puedes configurar notificaciones para que TransferV avise cuando una transferencia finaliza o falla.

Variables disponibles en el template:
* `{{status}}` — Estado (`completed`, `failed`, `transferring`)
* `{{torrent_name}}` — Nombre del torrent
* `{{torrent_hash}}` — Hash SHA1
* `{{source_path}}` — Ruta origen
* `{{destination_path}}` — Ruta destino
* `{{size}}` — Tamaño en bytes crudos
* `{{size_human}}` — Tamaño legible (ej: `1.50 GB`, `300.00 MB`)
* `{{message}}` — Mensaje (salida de rsync)
* `{{created_at}}` — Fecha de creación
* `{{completed_at}}` — Fecha de finalización

### Ejemplos de Integraciones

**WhatsApp (CallMeBot API gratis)**
* **URL:** `https://api.callmebot.com/whatsapp.php?phone=TU_NUMERO&text={{torrent_name}}+{{status}}&apikey=TU_APIKEY`
* **Método:** `GET`
* **Body:** `{}` (vacío)

**Telegram Bot API**
* **URL:** `https://api.telegram.org/bot<TOKEN>/sendMessage`
* **Método:** `POST`
* **Headers:** `{"Content-Type": "application/json"}`
* **Body:** `{"chat_id": "TU_CHAT_ID", "text": "✅ {{torrent_name}} → {{status}}"}`

**Discord Webhook**
* **URL:** Tu URL de webhook de Discord
* **Método:** `POST`
* **Headers:** `{"Content-Type": "application/json"}`
* **Body:** `{"content": "**TransferV** {{status}}: {{torrent_name}} → {{destination_path}}"}`

**Slack**
* **URL:** Tu Incoming Webhook URL
* **Método:** `POST`
* **Headers:** `{"Content-Type": "application/json"}`
* **Body:** `{"text": ":package: *{{torrent_name}}* — {{status}}"}`

**Ntfy.sh**
* **URL:** `https://ntfy.sh/TU_CANAL`
* **Método:** `POST`
* **Headers:** `{"Title": "TransferV", "Priority": "default"}`
* **Body:** `{{torrent_name}} — {{status}}`

## 📝 Notes

* The Docker image includes `rsync` and `openssh-client`.
* Password SSH auth uses `sshpass`; key auth is recommended.
* SSH key files should be mounted into the container and referenced from the settings screen.
* In `orchestrated_pull` mode, the destination server must be able to SSH into the VPS by the configured Tailscale host/name.
* The qBittorrent WebUI API must be reachable from where TransferV runs.
* Settings are stored in the local SQLite database under the mounted data directory.
