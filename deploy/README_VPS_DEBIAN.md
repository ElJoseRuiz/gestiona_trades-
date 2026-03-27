# Despliegue sencillo en Debian con systemd y Telegram

Este flujo esta pensado para una VPS Debian gestionada por SSH, como la que ya usas con Tailscale.

## 1. Preparar el sistema

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
```

## 2. Copiar el proyecto

```bash
sudo mkdir -p /opt/gestiona_trades
sudo chown "$USER":"$USER" /opt/gestiona_trades
cd /opt/gestiona_trades
git clone <TU_REPO_GIT> .
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 3. Crear la configuracion

```bash
cp config.example.yaml config.yaml
```

Ajusta al menos:

- `binance.api_key`
- `binance.api_secret`
- `signals.file_path`
- `notifications.enabled`
- `notifications.instance_name`
- `notifications.telegram.bot_token`
- `notifications.telegram.chat_id`

## 4. Instalar el servicio

```bash
sudo cp deploy/systemd/gestiona_trades.service.example /etc/systemd/system/gestiona_trades.service
sudo nano /etc/systemd/system/gestiona_trades.service
```

Revisa en el servicio:

- `User`
- `WorkingDirectory`
- `ExecStart`

Luego:

```bash
sudo systemctl daemon-reload
sudo systemctl enable gestiona_trades
sudo systemctl start gestiona_trades
```

## 5. Operacion diaria

```bash
sudo systemctl status gestiona_trades
sudo systemctl restart gestiona_trades
journalctl -u gestiona_trades -f
```

## 6. Que mensajes manda Telegram

Con la configuracion de ejemplo activa, el bot puede avisar de:

- arranque correcto
- parada del proceso
- errores del motor o errores fatales
- caida prolongada del WebSocket de Binance
- deteccion de posiciones huerfanas en Binance
- resumen periodico con abiertos, PnL, balance y estado del WS

## 7. Recomendacion practica

Para una primera fase, usa solo Telegram. Es mas simple que email, requiere menos mantenimiento en VPS y suele llegar antes para incidencias.
