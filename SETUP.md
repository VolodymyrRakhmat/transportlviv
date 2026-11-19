# Запуск на DigitalOcean (проєкт transit-monitor)

## 1. Створіть Droplet у проєкті
Проєкт transit-monitor → **Create → Droplets** (сторінка «Move resources» — це лише перенесення вже наявних ресурсів, вона вам не потрібна).
- Region: Frankfurt (або Amsterdam)
- Image: **Ubuntu 24.04 LTS**
- Plan: Basic → Regular → **1 GB RAM** (~$6/міс)
- Authentication: SSH key (краще) або пароль
- Створити, скопіювати IP.

## 2. Підключіться
Або з консолі Droplet у браузері (Droplet → Access → Launch Droplet Console), або: `ssh root@IP`

## 3. Підготовка сервера (під root)
```bash
apt update && apt install -y python3-venv python3-pip
adduser --system --group --home /opt/transit-monitor transit
```

## 4. Завантажте файли з вашого ПК
Покладіть папку з цими файлами та `serviceAccountKey.json` разом і виконайте на ПК (PowerShell/термінал):
```bash
scp provider.py requirements.txt serviceAccountKey.json root@IP:/opt/transit-monitor/
scp transit-monitor.service root@IP:/etc/systemd/system/
scp transit-monitor.env.example root@IP:/etc/transit-monitor.env
```

## 5. Залежності та права (на сервері)
```bash
cd /opt/transit-monitor
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
chown -R transit:transit /opt/transit-monitor
chmod 600 /opt/transit-monitor/serviceAccountKey.json /etc/transit-monitor.env
```

## 6. Впишіть секрети
```bash
nano /etc/transit-monitor.env
```
Вкажіть свою пошту та **новий** пароль застосунку Google.

## 7. Запуск
```bash
systemctl daemon-reload
systemctl enable --now transit-monitor
journalctl -u transit-monitor -f      # живі логи (вихід: Ctrl+C)
```
Має з'явитись `GTFS static: routes=...` і потім рядки `Оновлено N ТЗ`.

Корисне: `systemctl status transit-monitor`, `systemctl restart transit-monitor`.

## 8. Безпека (обов'язково)
- Старий пароль Gmail був у коді відкритим текстом — **відкличте його**: Google-акаунт → Безпека → Паролі застосунків.
- Якщо `serviceAccountKey.json` або старий `provider.py` потрапляли на GitHub — у Firebase Console → Project settings → Service accounts згенеруйте новий ключ, а старий видаліть.
