import requests, zipfile, time, json, firebase_admin, smtplib, math, re
from email.mime.text import MIMEText
from io import BytesIO
from firebase_admin import credentials, db
from datetime import datetime, timedelta
from google.transit import gtfs_realtime_pb2
import pandas as pd
import os

os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
os.environ.pop('http_proxy', None)
os.environ.pop('https_proxy', None)


session = requests.Session()
session.trust_env = False  

adapter = requests.adapters.HTTPAdapter(
    pool_connections=2,
    pool_maxsize=2,
    max_retries=3
)
session.mount("http://", adapter)
session.mount("https://", adapter)
# ============================================================================

# ================== НАЛАШТУВАННЯ ПОШТИ ==================
# Значення беруться зі змінних середовища (env vars), а не з коду,
# щоб пароль/адреси не потрапили у публічний репозиторій на GitHub.
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_TO = os.environ.get("EMAIL_TO", EMAIL_FROM)
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")

def send_email_alert(new_unknown):
    if not new_unknown:
        return
    if not EMAIL_FROM or not EMAIL_PASSWORD:
        print("⚠️ EMAIL_FROM/EMAIL_PASSWORD не задані — лист не надіслано")
        return
    subject = f"🚨 Нові неідентифіковані ТЗ: {len(new_unknown)}"
    body = "Знайдено нові неідентифіковані борти, які зараз на лінії:\n\n"
    for vid, info in new_unknown.items():
        body += f"ID: {vid} | Маршрут: {info.get('route', 'Невідомо')} | Час: {info.get('time', '')}\n"

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"✅ Лист надіслано ({len(new_unknown)} нових невідомих)")
    except Exception as e:
        print(f"❌ Помилка відправки листа: {e}")


def send_idle_alert_email(newly_short, newly_long, back_online):
    """Лист про борти, які щойно перетнули поріг простою (коротка/довга перерва),
    або навпаки — знову вийшли на лінію після простою."""
    if not (newly_short or newly_long or back_online):
        return
    if not EMAIL_FROM or not EMAIL_PASSWORD:
        print("⚠️ EMAIL_FROM/EMAIL_PASSWORD не задані — лист не надіслано")
        return

    subject_parts = []
    if newly_long:
        subject_parts.append(f"довгий простій: {len(newly_long)}")
    if newly_short:
        subject_parts.append(f"коротка перерва: {len(newly_short)}")
    if back_online:
        subject_parts.append(f"повернулись: {len(back_online)}")
    subject = "🚌 Автопарк — " + ", ".join(subject_parts)

    body = ""
    if newly_long:
        body += f"🔴 НЕ на лінії {IDLE_LONG_DAYS}+ днів:\n"
        for item in newly_long:
            body += f"  • {item['plate']} ({item['enterprise']}) — востаннє {item['last_seen'] or 'ніколи не бачили'}, {item['days']} дн.\n"
        body += "\n"
    if newly_short:
        body += f"🟡 НЕ на лінії {IDLE_SHORT_DAYS}-{IDLE_LONG_DAYS - 1} днів:\n"
        for item in newly_short:
            body += f"  • {item['plate']} ({item['enterprise']}) — востаннє {item['last_seen'] or 'ніколи не бачили'}, {item['days']} дн.\n"
        body += "\n"
    if back_online:
        body += "🟢 Знову на лінії (після простою):\n"
        for item in back_online:
            body += f"  • {item['plate']} ({item['enterprise']})\n"

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"✅ Лист про простій автопарку надіслано")
    except Exception as e:
        print(f"❌ Помилка відправки листа про простій: {e}")

# =======================================================

# ================ АВТОПАРК: НОРМАЛІЗАЦІЯ / ПОРОГИ ================
IDLE_SHORT_DAYS = 4   # від стількох днів без виходу — "коротка перерва" (жовтий)
IDLE_LONG_DAYS = 10   # від стількох днів без виходу — "довга перерва" (червоний)


def normalize_plate(raw):
    """Той самий принцип, що й у fleet_import.py / cleanBortName в admin.html,
    тільки без пробілів — так само, як колонка kstat/Kstat/stan в excel."""
    if not raw:
        return ''
    s = str(raw).strip().upper()
    s = re.sub(r'[^A-ZА-Я0-9]', '', s)
    return s


def normalize_to_slots(records):
    """Приводить запис vehicle_mapping/{raw_id} до списку слотів {p, from},
    сумісно зі старим форматом [p, l, c, m] (один запис без дати — діє завжди).
    Дзеркалить normalizeToSlots() з admin.html."""
    if not records:
        return []
    if isinstance(records, list) and records and isinstance(records[0], dict):
        return [{'p': r.get('p', ''), 'from': r.get('from', '')} for r in records[:3]]
    if isinstance(records, list) and records:
        return [{'p': records[0] or '', 'from': ''}]
    return []


def get_active_bort(records, date_str):
    """Повертає назву борту, що діяв на дату date_str, або None.
    Дзеркалить getActiveRecordForDate() з admin.html."""
    slots = [s for s in normalize_to_slots(records) if s['p']]
    if not slots:
        return None
    candidates = [s for s in slots if not s['from'] or s['from'] <= date_str]
    if not candidates:
        return None
    candidates.sort(key=lambda s: s['from'] or '', reverse=True)
    return candidates[0]['p']
# ===================================================================

DB_URL = os.environ.get('FIREBASE_DB_URL', 'https://lviv-transport-web-default-rtdb.firebaseio.com/')
STATIC_URL = "http://track.ua-gis.com/gtfs/lviv/static.zip"
REALTIME_URL = "http://track.ua-gis.com/gtfs/lviv/vehicle_position"
CERT_FILE = "serviceAccountKey.json"

def _load_firebase_credentials():
    """На Render ключ сервісного акаунта передається через змінну середовища
    FIREBASE_CREDENTIALS_JSON (весь вміст serviceAccountKey.json одним рядком),
    бо сам файл у git не комітиться. Локально, якщо такий файл лежить поруч
    зі скриптом, можна користуватись ним для зручності розробки."""
    cred_json = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if cred_json:
        return credentials.Certificate(json.loads(cred_json))
    if os.path.exists(CERT_FILE):
        return credentials.Certificate(CERT_FILE)
    raise RuntimeError(
        "Не знайдено облікових даних Firebase: задайте змінну середовища "
        "FIREBASE_CREDENTIALS_JSON або покладіть serviceAccountKey.json поруч зі скриптом."
    )

if not firebase_admin._apps:
    firebase_admin.initialize_app(_load_firebase_credentials(), {'databaseURL': DB_URL})

def load_mapping():
    try:
        mapping = db.reference('vehicle_mapping').get()
        return mapping if mapping else {}
    except Exception as e:
        print(f"Не вдалося завантажити mapping з Firebase: {e}")
        return {}

def get_routes_map():
    try:
        r = session.get(STATIC_URL, timeout=30)
        z = zipfile.ZipFile(BytesIO(r.content))
        with z.open('routes.txt') as f:
            routes = pd.read_csv(f)
            return {str(row['route_id']).strip(): str(row['route_short_name']).strip() for _, row in routes.iterrows()}
    except Exception as e:
        print(f"Помилка завантаження routes.txt: {e}")
        return {}

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000  # метри
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def check_fleet_idle(today_str):
    """Раз на добу: звіряє статичний реєстр автопарку (fleet, з fleet_import.py)
    з тим, коли борт востаннє реально бачили на лінії (last_seen), і шле лист
    тільки про ЗМІНИ стану (щоб не спамити щодня одним і тим самим списком)."""
    fleet = db.reference('fleet').get() or {}
    last_seen = db.reference('last_seen').get() or {}
    alert_state = db.reference('idle_alert_state').get() or {}

    today = datetime.strptime(today_str, "%Y-%m-%d")
    newly_short, newly_long, back_online = [], [], []
    new_alert_state = {}

    for ent_key, buses in fleet.items():
        for key, info in (buses or {}).items():
            seen = last_seen.get(key)
            if seen and seen.get('date'):
                days = (today - datetime.strptime(seen['date'], "%Y-%m-%d")).days
            else:
                days = None  # ніколи не бачили в трекінгу

            if days is None or days >= IDLE_LONG_DAYS:
                level = 'long'
            elif days >= IDLE_SHORT_DAYS:
                level = 'short'
            else:
                level = 'ok'

            prev_level = (alert_state.get(key) or {}).get('level')
            item = {
                'plate': info.get('plate', key),
                'enterprise': info.get('enterprise_name', ent_key),
                'last_seen': seen.get('date') if seen else None,
                'days': days if days is not None else '∞',
            }

            if level != prev_level:
                if level == 'long':
                    newly_long.append(item)
                elif level == 'short':
                    newly_short.append(item)
                elif level == 'ok' and prev_level in ('short', 'long'):
                    back_online.append(item)

            if level == 'ok':
                continue  # не тримаємо в idle_alert_state борти, які й так на лінії
            new_alert_state[key] = {'level': level, 'since': today_str}

    db.reference('idle_alert_state').set(new_alert_state)
    send_idle_alert_email(newly_short, newly_long, back_online)
    print(f"[АВТОПАРК] Перевірка простою за {today_str}: "
          f"нових коротких={len(newly_short)}, нових довгих={len(newly_long)}, повернулись={len(back_online)}")


def run():
    routes_map = get_routes_map()
    ref_live = db.reference('vehicles')
    ref_alerts = db.reference('alerts')
    ref_history = db.reference('history')
    ref_last_seen = db.reference('last_seen')
    nonlocal_state = {'last_idle_check_date': None, 'today': None}

    trolley_numbers = ['22', '23', '24', '25', '30', '31', '32', '33', '38']

    last_history_save = 0
    HISTORY_INTERVAL = 900           # 15 хвилин
    last_cleanup = 0
    CLEANUP_INTERVAL = 60

    last_alerted = set()
    prev_state = {}                  # raw_id -> стан для контролю запису

    STATIONARY_THRESHOLD = 8        # ~1.5 хвилини для невідомих
    CHANGE_THRESHOLD_METERS = 20

    # Раз на скільки циклів пере-створювати сесію "про всяк випадок"
    # (додатковий страховий запобіжник, не обов'язковий за наявності Session, але не заважає)
    cycle_count = 0
    SESSION_REFRESH_EVERY = 2000  # ~6 годин при інтервалі 11с

    while True:
        try:
            cycle_count += 1
            if cycle_count % SESSION_REFRESH_EVERY == 0:
                session.close()
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                print("[INFO] Сесію HTTP перестворено (профілактика)")

            vehicle_mapping = load_mapping()
            res = session.get(REALTIME_URL, timeout=(10, 60))

            if res.status_code == 200:
                try:
                    feed = gtfs_realtime_pb2.FeedMessage()
                    feed.ParseFromString(res.content)
                except Exception as parse_e:
                    print(f"❌ Помилка розбору protobuf: {parse_e}")
                    time.sleep(11)
                    continue
                finally:
                    res.close()

                current_ts = int(time.time())
                current_date = datetime.now().strftime("%Y-%m-%d")
                current_hour = datetime.now().strftime("%H")
                current_time_str = datetime.now().strftime("%H:%M:%S")

                # ==================== АВТОПАРК: ОСТАННЄ ПОБАЧЕННЯ (кожен цикл) ====================
                # Раніше last_seen оновлювався РАЗОМ з history — тобто лише раз
                # на HISTORY_INTERVAL (5 хв). last_seen, на відміну від history,
                # не накопичується (це просто перезапис по ключу борту), тому
                # немає причини тримати його на такому ж рідкому інтервалі.
                # Через цю затримку підмінний борт, який щойно виїхав (типовий
                # випадок — заміна на другу зміну), в автопарку "оживав" із
                # запізненням до 5 хвилин. Тепер пишемо його щоцикл (~11с),
                # одразу як прийшли свіжі дані з треку.
                last_seen_payload = {}
                for entity in feed.entity:
                    if not entity.HasField('vehicle'): continue
                    v = entity.vehicle
                    raw_id = str(getattr(v.vehicle, 'id', '')).strip()
                    if not raw_id: continue

                    r_id = str(v.trip.route_id).strip()
                    name = routes_map.get(r_id, r_id)

                    bort_name = get_active_bort(vehicle_mapping.get(raw_id), current_date)
                    if bort_name:
                        key = normalize_plate(bort_name)
                        if key:
                            last_seen_payload[key] = {
                                'plate': bort_name,
                                'date': current_date,
                                'time': current_time_str,
                                'route': name,
                                'ts': current_ts,
                            }

                if last_seen_payload:
                    ref_last_seen.update(last_seen_payload)
                # ====================================================================================

                # ==================== ЗАПИС В HISTORY ====================
                if current_ts - last_history_save >= HISTORY_INTERVAL:
                    history_payload = {}
                    for entity in feed.entity:
                        if not entity.HasField('vehicle'): continue
                        v = entity.vehicle
                        raw_id = str(getattr(v.vehicle, 'id', '')).strip()
                        if not raw_id: continue

                        r_id = str(v.trip.route_id).strip()
                        name = routes_map.get(r_id, r_id)

                        vehicle_data = {
                            'r': name,
                            'raw_p': raw_id,
                            'p': vehicle_mapping.get(raw_id, raw_id),
                            'lat': float(v.position.latitude),
                            'lng': float(v.position.longitude),
                            's': round(v.position.speed * 3.6, 1) if v.position.HasField('speed') else 0,
                            'timestamp': current_time_str
                        }
                        history_payload[raw_id] = vehicle_data

                    if history_payload:
                        ref_history.child(current_date).child(current_hour).update(history_payload)
                        print(f"[HISTORY] Записано {current_date} {current_hour}:00 — {len(history_payload)} ТЗ")
                        last_history_save = current_ts
                # =================================================================

                # ==================== АВТОПАРК: ЩОДЕННА ПЕРЕВІРКА ПРОСТОЮ ====================
                nonlocal_state['today'] = current_date
                if nonlocal_state['last_idle_check_date'] != current_date:
                    try:
                        check_fleet_idle(current_date)
                    except Exception as idle_e:
                        print(f"❌ Помилка перевірки простою автопарку: {idle_e}")
                    nonlocal_state['last_idle_check_date'] = current_date
                # ================================================================================

                # ==================== CLEANUP ====================
                if current_ts - last_cleanup >= CLEANUP_INTERVAL and len(feed.entity) > 0:
                    existing = ref_live.get() or {}
                    feed_ids = {str(getattr(e.vehicle.vehicle, 'id', '')).strip()
                             for e in feed.entity if e.HasField('vehicle')}

                    to_delete = []
                    for veh_id, data in existing.items():
                        if veh_id not in feed_ids:
                            last_upd = int(data.get('last_updated', 0))
                            if current_ts - last_upd > 180:
                                to_delete.append(veh_id)

                    for vid in to_delete:
                        ref_live.child(vid).delete()

                    if to_delete:
                        print(f"[ОЧИЩЕННЯ] Видалено привидів: {len(to_delete)}")
                    last_cleanup = current_ts
                # =================================================

                payload = {}
                alerts_payload = {}
                new_unknown_this_cycle = {}

                for entity in feed.entity:
                    if not entity.HasField('vehicle'): continue

                    v = entity.vehicle
                    raw_id = str(getattr(v.vehicle, 'id', '')).strip()
                    if not raw_id: continue

                    r_id = str(v.trip.route_id).strip()
                    name = routes_map.get(r_id, r_id)
                    is_identified = raw_id in vehicle_mapping

                    name_low = name.lower()
                    if name in trolley_numbers or 'трол' in name_low or 'тр' in name_low:
                        v_type = 'trolley'
                    elif 'трам' in name_low or name.startswith('Т'):
                        v_type = 'tram'
                    else:
                        v_type = 'bus'

                    current_lat = float(v.position.latitude)
                    current_lng = float(v.position.longitude)
                    current_speed = round(v.position.speed * 3.6, 1) if v.position.HasField('speed') else 0
                    stop = v.stop_id if v.HasField('stop_id') else "В дорозі"
                    gtfs_timestamp = v.timestamp if v.HasField('timestamp') else current_ts

                    # === Логіка зменшення дублювання ===
                    prev = prev_state.get(raw_id, {})
                    stationary_count = prev.get('stationary_count', 0)

                    moved = True
                    if prev:
                        dist = haversine(prev['lat'], prev['lng'], current_lat, current_lng)
                        if dist < CHANGE_THRESHOLD_METERS and abs(current_speed - prev.get('speed', 0)) < 2:
                            stationary_count += 1
                            moved = False
                        else:
                            stationary_count = 0

                    prev_state[raw_id] = {
                        'lat': current_lat,
                        'lng': current_lng,
                        'speed': current_speed,
                        'stationary_count': stationary_count,
                        'last_save_ts': prev.get('last_save_ts', 0)
                    }

                    # Рішення про запис
                    should_update_live = True
                    if not is_identified:
                        if stationary_count >= STATIONARY_THRESHOLD and current_speed < 3:
                            should_update_live = False
                    else:
                        if stationary_count >= 4 and current_speed < 3:
                            should_update_live = (current_ts - prev.get('last_save_ts', 0) > 60)

                    vehicle_data = {
                        'r': name,
                        'type': v_type,
                        'lat': current_lat,
                        'lng': current_lng,
                        'p': vehicle_mapping.get(raw_id, raw_id),
                        'raw_p': raw_id,
                        'b': float(v.position.bearing) if v.position.HasField('bearing') else 0.0,
                        's': current_speed,
                        'stop': stop,
                        'has_bort': is_identified,
                        'timestamp': current_time_str,
                        'last_updated': gtfs_timestamp,
                        'updated_at': datetime.now().isoformat()
                    }

                    if should_update_live:
                        payload[raw_id] = vehicle_data
                        prev_state[raw_id]['last_save_ts'] = current_ts

                    if not is_identified:
                        alerts_payload[raw_id] = {'id': raw_id, 'route': name, 'time': current_time_str}
                        if (current_speed > 3 or not v.HasField('stop_id')) and raw_id not in last_alerted:
                            new_unknown_this_cycle[raw_id] = alerts_payload[raw_id]
                            last_alerted.add(raw_id)

                # Запис в Firebase
                if payload:
                    ref_live.update(payload)

                ref_alerts.set(alerts_payload)

                if new_unknown_this_cycle:
                    send_email_alert(new_unknown_this_cycle)

                print(f"[{current_time_str}] Оновлено {len(payload)} ТЗ | Невідомих: {len(alerts_payload)} | Всього в prev_state: {len(prev_state)}")
            else:
                res.close()

            time.sleep(11)

        except Exception as e:
            print(f"Помилка: {e}")
            time.sleep(11)

if __name__ == "__main__":
    run()