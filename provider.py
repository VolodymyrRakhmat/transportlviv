import requests, zipfile, time, json, firebase_admin, smtplib, math, re, csv, io, threading
from email.mime.text import MIMEText
from io import BytesIO
from firebase_admin import credentials, db
from datetime import datetime, timedelta
from google.transit import gtfs_realtime_pb2
from zoneinfo import ZoneInfo
import os, gc, sys

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
# Усі секрети беруться зі змінних середовища (файл /etc/transit-monitor.env
# на сервері) — у коді та в git їх НЕ має бути.
EMAIL_FROM = os.environ.get('EMAIL_FROM', '')
EMAIL_TO = os.environ.get('EMAIL_TO', '') or EMAIL_FROM
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', '')

# Часовий пояс для дат/годин у Firebase (history, last_seen, простій автопарку)
TZ = ZoneInfo(os.environ.get('APP_TIMEZONE', 'Europe/Kyiv'))


def now():
    return datetime.now(TZ)

# ================== СПОВІЩЕННЯ ==================
# DigitalOcean блокує вихідний SMTP (порти 25/465/587), тому лист через Gmail SMTP
# звідти не піде. Використовуємо HTTPS-канали (вони не блокуються):
#   • Telegram-бот  — TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
#   • Resend (лист) — RESEND_API_KEY (+ EMAIL_TO; RESEND_FROM за замовчуванням onboarding@resend.dev)
# Якщо жоден не налаштований — пробуємо старий SMTP (з таймаутом), якщо він доступний.
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
RESEND_FROM = os.environ.get('RESEND_FROM', 'Transit Monitor <onboarding@resend.dev>')


def _deliver(subject, body):
    """Надсилає повідомлення всіма налаштованими каналами. Виконується у фоновому
    потоці, тож ніколи не зупиняє основний цикл оновлення карти."""
    sent = False
    https_configured = False

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        https_configured = True
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={'chat_id': TELEGRAM_CHAT_ID, 'text': f"{subject}\n\n{body}"[:4000]},
                timeout=15)
            r.raise_for_status()
            print("✅ Telegram: повідомлення надіслано")
            sent = True
        except Exception as e:
            print(f"❌ Telegram: помилка відправки: {e}")

    if RESEND_API_KEY and EMAIL_TO:
        https_configured = True
        try:
            r = requests.post(
                "https://api.resend.com/emails",
                headers={'Authorization': f'Bearer {RESEND_API_KEY}'},
                json={'from': RESEND_FROM, 'to': [EMAIL_TO], 'subject': subject, 'text': body},
                timeout=15)
            r.raise_for_status()
            print(f"✅ Resend: лист надіслано на {EMAIL_TO}")
            sent = True
        except Exception as e:
            print(f"❌ Resend: помилка відправки: {e}")

    if not https_configured and EMAIL_FROM and EMAIL_PASSWORD:
        try:
            msg = MIMEText(body)
            msg['Subject'] = subject
            msg['From'] = EMAIL_FROM
            msg['To'] = EMAIL_TO
            with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as server:
                server.login(EMAIL_FROM, EMAIL_PASSWORD)
                server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
            print("✅ SMTP: лист надіслано")
            sent = True
        except Exception as e:
            print(f"❌ SMTP: помилка відправки (на DigitalOcean порт заблоковано): {e}")

    if not sent and not https_configured and not (EMAIL_FROM and EMAIL_PASSWORD):
        print("⚠️ Канали сповіщень не налаштовані — повідомлення не надіслано")
    return sent


def notify(subject, body):
    threading.Thread(target=_deliver, args=(subject, body), daemon=True).start()


def send_email_alert(new_unknown):
    if not new_unknown:
        return
    subject = f"🚨 Нові неідентифіковані ТЗ: {len(new_unknown)}"
    body = "Знайдено нові неідентифіковані борти, які зараз на лінії:\n\n"
    for vid, info in new_unknown.items():
        body += f"ID: {vid} | Маршрут: {info.get('route', 'Невідомо')} | Час: {info.get('time', '')}\n"
    notify(subject, body)


def send_idle_alert_email(newly_short, newly_long, back_online):
    """Лист про борти, які щойно перетнули поріг простою (коротка/довга перерва),
    або навпаки — знову вийшли на лінію після простою."""
    if not (newly_short or newly_long or back_online):
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

    notify(subject, body)


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
    Дзеркалить normalizeRecords() з vehical.html.
    УВАГА: раніше тут стояв records[:3] — через це 4-й і далі записи історії
    (нові перестановки трекера) ігнорувались колектором. Тепер беремо всі."""
    if not records:
        return []
    if isinstance(records, dict):  # Firebase іноді віддає список як {"0":..,"1":..}
        records = [records[k] for k in sorted(records, key=lambda x: int(x) if str(x).isdigit() else 0)]
    if isinstance(records, list) and records and isinstance(records[0], dict):
        return [{'p': r.get('p', ''), 'from': r.get('from', '') or ''} for r in records]
    if isinstance(records, list) and records:
        return [{'p': records[0] or '', 'from': ''}]
    return []


def _from_key(s):
    """'' -> '' (діє завжди); 'РРРР-ММ-ДД' -> 'РРРР-ММ-ДД 00:00';
    'РРРР-ММ-ДД ГГ:ХХ' лишається як є. Так дата і дата+час порівнюються однаково."""
    s = (s or '').strip()
    if not s:
        return ''
    if len(s) == 10:
        return s + ' 00:00'
    return s[:16]


def get_active_bort(records, when_str):
    """Повертає назву борту, що діяв на момент when_str ('РРРР-ММ-ДД' або
    'РРРР-ММ-ДД ГГ:ХХ'), або None. Дзеркалить getActiveIndex() з vehical.html:
    серед записів з непорожнім номером і \"з\" <= моменту обирається найпізніший
    \"з\"; при рівних — доданий пізніше (більший індекс)."""
    when = _from_key(when_str) if len(when_str or '') == 10 else (when_str or '')[:16]
    best = None
    best_from = None
    for sl in normalize_to_slots(records):
        if not sl['p']:
            continue
        f = _from_key(sl['from'])
        if f and f > when:
            continue
        if best is None or f >= best_from:
            best, best_from = sl['p'], f
    return best
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
        return None  # None = помилка, лишаємо попередній кеш

def clean_headsign(text):
    """Прибирає з напрямку рейсу зайві цифрові позначки, які не потрібні в UI:
    - цифровий префікс маршруту на початку ('12 ', '12-', '12: ')
    - службовий код у дужках в кінці ('Вікторія Гарденс (547)' -> 'Вікторія Гарденс')
    - службовий код через тире/крапку в кінці ('Вікторія Гарденс - 547')
    Лишає тільки текстову назву кінцевої/напрямку."""
    if not text:
        return ''
    cleaned = str(text).strip()
    if cleaned.lower() == 'nan':
        return ''
    cleaned = re.sub(r'^\s*\d+\s*[-–—:.]*\s*', '', cleaned)
    cleaned = re.sub(r'\s*\(\s*\d+\s*\)\s*$', '', cleaned)
    cleaned = re.sub(r'\s*[-–—]\s*\d+\s*$', '', cleaned)
    return cleaned.strip()


def _read_csv(z, name):
    with z.open(name) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig', newline='')))


def get_routes_map():
    """Повертає (routes_map, headsign_map, trip_shape_map, shape_map, trip_direction_map):
    - routes_map: route_id -> короткий номер маршруту (routes.txt)
    - headsign_map: trip_id -> кінцева/напрямок рейсу (trips.txt, без цифр)
    - trip_shape_map: trip_id -> shape_id (trips.txt)
    - shape_map: shape_id -> список (lat, lng) точок траєкторії (shapes.txt)
    - trip_direction_map: trip_id -> direction_id (0/1): з trips.txt, а якщо там
      його нема — за порядком появи різних кінцевих у межах маршруту.
    Без pandas (csv) — менше пам'яті та швидше на маленькому сервері."""
    try:
        r = session.get(STATIC_URL, timeout=60)
        z = zipfile.ZipFile(BytesIO(r.content))

        routes_map = {}
        for row in _read_csv(z, 'routes.txt'):
            rid = (row.get('route_id') or '').strip()
            if rid:
                routes_map[rid] = (row.get('route_short_name') or '').strip()

        headsign_map = {}
        trip_shape_map = {}
        trip_direction_map = {}
        route_headsign_order = {}
        trip_headsign_by_route = {}
        try:
            for row in _read_csv(z, 'trips.txt'):
                trip_id = (row.get('trip_id') or '').strip()
                if not trip_id:
                    continue
                route_id = (row.get('route_id') or '').strip()

                headsign = clean_headsign(row.get('trip_headsign'))
                if headsign:
                    headsign_map[trip_id] = headsign
                    if route_id:
                        order = route_headsign_order.setdefault(route_id, [])
                        if headsign not in order:
                            order.append(headsign)
                        trip_headsign_by_route[trip_id] = (route_id, headsign)

                shape_id = (row.get('shape_id') or '').strip()
                if shape_id:
                    trip_shape_map[trip_id] = shape_id

                direction_id = (row.get('direction_id') or '').strip()
                if direction_id:
                    try:
                        trip_direction_map[trip_id] = int(float(direction_id))
                    except ValueError:
                        pass

            for trip_id, (route_id, headsign) in trip_headsign_by_route.items():
                if trip_id in trip_direction_map:
                    continue
                order = route_headsign_order.get(route_id, [])
                if headsign in order:
                    trip_direction_map[trip_id] = order.index(headsign) % 2
        except Exception as e:
            print(f"⚠️ Не вдалося завантажити trips.txt (напрямки рейсів): {e}")

        shape_map = {}
        try:
            raw_shapes = {}
            for row in _read_csv(z, 'shapes.txt'):
                sid = (row.get('shape_id') or '').strip()
                if not sid:
                    continue
                raw_shapes.setdefault(sid, []).append((
                    float(row['shape_pt_sequence']),
                    float(row['shape_pt_lat']),
                    float(row['shape_pt_lon']),
                ))
            for sid, pts in raw_shapes.items():
                pts.sort(key=lambda t: t[0])
                points = [(lat, lon) for _, lat, lon in pts]
                if len(points) > 300:
                    step = len(points) // 300
                    points = points[::step]
                shape_map[sid] = points
        except Exception as e:
            print(f"⚠️ Не вдалося завантажити shapes.txt (перевірка 'поза маршрутом' вимкнена): {e}")

        print(f"📍 GTFS static: routes={len(routes_map)}, headsigns={len(headsign_map)}, "
              f"trip→shape={len(trip_shape_map)}, shapes={len(shape_map)}, trip→direction={len(trip_direction_map)}")
        if not trip_direction_map:
            print("⚠️ direction_id відсутній у trips.txt — підсвітка напрямку працюватиме лише з realtime-фіда.")
        if not trip_shape_map or not shape_map:
            print("⚠️ shape_id/shapes.txt відсутні — перевірка 'поза маршрутом' не працюватиме.")

        return routes_map, headsign_map, trip_shape_map, shape_map, trip_direction_map
    except Exception as e:
        print(f"Помилка завантаження GTFS static: {e}")
        return {}, {}, {}, {}, {}


def _point_segment_distance_m(lat, lon, lat1, lon1, lat2, lon2):
    """Відстань (у метрах) від точки до відрізка, у пласкій проекції навколо
    середньої широти відрізка (достатньо точно на масштабах міста)."""
    ref_lat = math.radians((lat1 + lat2) / 2)
    mx = 111320 * math.cos(ref_lat)
    my = 111320

    px, py = lon * mx, lat * my
    x1, y1 = lon1 * mx, lat1 * my
    x2, y2 = lon2 * mx, lat2 * my

    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)

    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = x1 + t * dx, y1 + t * dy
    return math.hypot(px - cx, py - cy)


def distance_to_shape_m(lat, lon, shape_points):
    """Мінімальна відстань від точки (lat, lon) до ламаної траєкторії маршруту."""
    if not shape_points or len(shape_points) < 2:
        return None
    min_d = float('inf')
    for i in range(len(shape_points) - 1):
        lat1, lon1 = shape_points[i]
        lat2, lon2 = shape_points[i + 1]
        d = _point_segment_distance_m(lat, lon, lat1, lon1, lat2, lon2)
        if d < min_d:
            min_d = d
    return min_d


OFF_ROUTE_THRESHOLD_METERS = 100  # далі стільки метрів від траєкторії — вважаємо "поза маршрутом"



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



# ===================== НІЧНИЙ РЕЖИМ (відпочинок сервера) =====================
# З NIGHT_START_HOUR до NIGHT_END_HOUR (за київським часом) збір зупиняється:
# живі позиції очищаються, а на виході з режиму процес перезапускається сам
# (os.execv) — це повністю звільняє пам'ять, що накопичується за 2-3 доби.
NIGHT_ENABLED = os.environ.get('NIGHT_ENABLED', '1') == '1'
NIGHT_START_HOUR = int(os.environ.get('NIGHT_START_HOUR', '0'))
NIGHT_END_HOUR = int(os.environ.get('NIGHT_END_HOUR', '4'))
NIGHT_RESTART = os.environ.get('NIGHT_RESTART', '1') == '1'

def in_night():
    h = now().hour
    if NIGHT_START_HOUR <= NIGHT_END_HOUR:
        return NIGHT_START_HOUR <= h < NIGHT_END_HOUR
    return h >= NIGHT_START_HOUR or h < NIGHT_END_HOUR

def night_rest():
    print(f"[NIGHT] Нічний режим {NIGHT_START_HOUR:02d}:00-{NIGHT_END_HOUR:02d}:00 — пауза")
    try:
        db.reference('/').update({'vehicles': None, 'alerts': None,
            'control/night_mode': {'active': True, 'until': f"{NIGHT_END_HOUR:02d}:00", 'ts': int(time.time())}})
    except Exception as e:
        print(f"[NIGHT] Не вдалося оновити Firebase: {e}")
    gc.collect()
    while in_night():
        time.sleep(60)
    try:
        db.reference('control/night_mode').set({'active': False, 'ts': int(time.time())})
    except Exception:
        pass
    print("[NIGHT] Ранок — перезапуск процесу для чистої пам'яті")
    if NIGHT_RESTART:
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            print(f"[NIGHT] execv не вдався ({e}), продовжуємо без перезапуску")

def run():
    routes_map, headsign_map, trip_shape_map, shape_map, trip_direction_map = get_routes_map()
    ref_live = db.reference('vehicles')
    ref_alerts = db.reference('alerts')
    ref_history = db.reference('history')
    ref_last_seen = db.reference('last_seen')
    nonlocal_state = {'last_idle_check_date': None, 'today': None}

    trolley_numbers = ['22', '23', '24', '25', '30', '31', '32', '33', '38']

    last_history_save = 0
    HISTORY_INTERVAL = 900           # 15 хвилин
    last_cleanup = 0
    startup_cleanup_done = False
    CLEANUP_INTERVAL = 60

    last_alerted = set()
    prev_state = {}                  # raw_id -> стан для контролю запису

    STATIONARY_THRESHOLD = 8        # ~1.5 хвилини для невідомих
    CHANGE_THRESHOLD_METERS = 20

    # Раз на скільки циклів пере-створювати сесію "про всяк випадок"
    # (додатковий страховий запобіжник, не обов'язковий за наявності Session, але не заважає)
    cycle_count = 0
    vehicle_mapping = {}
    mapping_loaded = False
    last_mapping_load = 0
    last_reload_token = None
    last_control_poll = 0
    CONTROL_POLL_SECONDS = int(os.environ.get('CONTROL_POLL_SECONDS', '30'))
    MAPPING_REFRESH_SECONDS = int(os.environ.get('MAPPING_REFRESH_SECONDS', '1800'))
    seen_ts = {}                     # raw_id -> коли востаннє був у фіді (для очищення)
    last_static_refresh = time.time()
    STATIC_REFRESH_INTERVAL = 24 * 3600
    SESSION_REFRESH_EVERY = 2000  # ~6 годин при інтервалі 11с

    while True:
        if NIGHT_ENABLED and in_night():
            night_rest()
            prev_state.clear(); seen_ts.clear(); last_alerted.clear(); gc.collect()
            continue
        cycle_start = time.time()
        tm = {}   # діагностика: скільки секунд зайняла кожна операція циклу
        try:
            cycle_count += 1
            if cycle_count % SESSION_REFRESH_EVERY == 0:
                session.close()
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                print("[INFO] Сесію HTTP перестворено (профілактика)")

            if time.time() - last_static_refresh >= STATIC_REFRESH_INTERVAL:
                fresh = get_routes_map()
                if fresh[0]:
                    routes_map, headsign_map, trip_shape_map, shape_map, trip_direction_map = fresh
                    print("[INFO] GTFS static оновлено")
                last_static_refresh = time.time()

            # mapping перечитуємо: (а) по кнопці в адмінці — вона пише мітку часу
            # у control/mapping_reload (крихітний вузол, читання майже безкоштовне);
            # (б) страхово — раз на MAPPING_REFRESH_SECONDS, якщо про кнопку забули.
            reload_token = last_reload_token
            if time.time() - last_control_poll >= CONTROL_POLL_SECONDS:
                last_control_poll = time.time()
                try:
                    _t = time.time()
                    reload_token = db.reference('control/mapping_reload').get()
                    tm['ctl'] = time.time() - _t
                except Exception:
                    reload_token = last_reload_token
            force_reload = reload_token != last_reload_token
            if (not mapping_loaded or force_reload
                    or time.time() - last_mapping_load >= MAPPING_REFRESH_SECONDS):
                fresh_map = load_mapping()
                if fresh_map is not None:
                    vehicle_mapping = fresh_map
                    mapping_loaded = True
                    last_reload_token = reload_token
                    print(f"[MAPPING] Оновлено ({len(vehicle_mapping)} записів)"
                          f"{' — по кнопці' if force_reload else ''}")
                last_mapping_load = time.time()
            fetch_start = time.time()
            res = session.get(REALTIME_URL, timeout=(10, 60))
            fetch_s = time.time() - fetch_start

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
                current_date = now().strftime("%Y-%m-%d")
                current_dt = now().strftime("%Y-%m-%d %H:%M")   # для мапінгу (борт на цей момент)
                current_hour = now().strftime("%H")
                current_time_str = now().strftime("%H:%M:%S")

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
                    seen_ts[raw_id] = current_ts

                    r_id = str(v.trip.route_id).strip()
                    name = routes_map.get(r_id, r_id)

                    bort_name = get_active_bort(vehicle_mapping.get(raw_id), current_dt)
                    if bort_name:
                        key = normalize_plate(bort_name)
                        if key:
                            last_seen_payload[key] = {
                                'plate': bort_name,
                                'date': current_date,
                                'time': current_time_str,
                                'route': name,
                                'ts': current_ts,
                                'raw': raw_id,   # на якому трекері бачили (для історії перестановок)
                            }

                # last_seen пишемо разом з рештою в ОДНОМУ запиті нижче (див. "Запис в Firebase")
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
                            'plate': get_active_bort(vehicle_mapping.get(raw_id), current_dt) or '',
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
                    feed_ids = {str(getattr(e.vehicle.vehicle, 'id', '')).strip()
                                for e in feed.entity if e.HasField('vehicle')}
                    to_delete = set()

                    # Одноразово після (пере)запуску: звіряємось зі списком ключів у базі
                    # (shallow = лише ключі, а не весь вузол) — прибираємо "привидів",
                    # що лишились від попереднього запуску.
                    if not startup_cleanup_done:
                        existing_keys = ref_live.get(shallow=True) or {}
                        to_delete |= {k for k in existing_keys if k not in feed_ids}
                        startup_cleanup_done = True

                    # Далі — БЕЗ жодного читання з Firebase: самі пам'ятаємо, кого
                    # писали, і видаляємо тих, кого нема у фіді понад 3 хвилини.
                    for vid, t in list(seen_ts.items()):
                        if vid not in feed_ids and current_ts - t > 180:
                            to_delete.add(vid)

                    for vid in to_delete:
                        ref_live.child(vid).delete()
                        seen_ts.pop(vid, None)

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

                    trip_id = str(getattr(v.trip, 'trip_id', '')).strip()
                    dest = headsign_map.get(trip_id, '')

                    # direction_id: 0 = "туди", 1 = "назад" (як у полях схеми на фронті).
                    # Спочатку пробуємо з самого realtime-фіда (найсвіжіше), якщо там
                    # немає — підстраховуємось мапою з trips.txt.
                    if v.trip.HasField('direction_id'):
                        direction_id = int(v.trip.direction_id)
                    else:
                        direction_id = trip_direction_map.get(trip_id)

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

                    # ==== перевірка "поза маршрутом" ====
                    off_route = False
                    shape_pts = shape_map.get(trip_shape_map.get(trip_id, ''))
                    if shape_pts:
                        dist_to_route = distance_to_shape_m(current_lat, current_lng, shape_pts)
                        if dist_to_route is not None and dist_to_route > OFF_ROUTE_THRESHOLD_METERS:
                            off_route = True


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
                        'p': get_active_bort(vehicle_mapping.get(raw_id), current_dt) or raw_id,
                        'raw_p': raw_id,
                        'dest': dest,
                        'dir': direction_id,
                        'off_route': off_route,
                        'b': float(v.position.bearing) if v.position.HasField('bearing') else 0.0,
                        's': current_speed,
                        'stop': stop,
                        'has_bort': is_identified,
                        'timestamp': current_time_str,
                        'last_updated': gtfs_timestamp,
                        'updated_at': now().isoformat()
                    }

                    if should_update_live:
                        payload[raw_id] = vehicle_data
                        prev_state[raw_id]['last_save_ts'] = current_ts

                    if not is_identified:
                        alerts_payload[raw_id] = {'id': raw_id, 'route': name, 'time': current_time_str}
                        if (current_speed > 3 or not v.HasField('stop_id')) and raw_id not in last_alerted:
                            new_unknown_this_cycle[raw_id] = alerts_payload[raw_id]
                            last_alerted.add(raw_id)

                # Запис в Firebase — ОДИН запит на цикл (multi-path update).
                # Кожен окремий запит з цього сервера коштує дорого (нове TLS-з'єднання
                # ~2.5 с), тому vehicles + last_seen + alerts відправляємо разом.
                updates = {}
                for vid, data in payload.items():
                    updates[f'vehicles/{vid}'] = data
                for key, data in last_seen_payload.items():
                    updates[f'last_seen/{key}'] = data
                updates['alerts'] = alerts_payload or None   # None = очистити вузол

                _t = time.time()
                db.reference('/').update(updates)
                tm['write'] = time.time() - _t

                if new_unknown_this_cycle:
                    send_email_alert(new_unknown_this_cycle)

                cycle_s = time.time() - cycle_start
                print(f"[{current_time_str}] Оновлено {len(payload)} ТЗ | Невідомих: {len(alerts_payload)} | "
                      f"prev_state: {len(prev_state)} | ⏱ фід {fetch_s:.1f}с, цикл {cycle_s:.1f}с | "
                      + ", ".join(f"{k} {v:.1f}с" for k, v in tm.items()))
            else:
                res.close()

            # Крок цикл→цикл = 11 с (а не 11 с ПІСЛЯ роботи): віднімаємо час, який вже пішов.
            time.sleep(max(1, 11 - (time.time() - cycle_start)))

        except Exception as e:
            print(f"Помилка: {e}")
            time.sleep(11)

if __name__ == "__main__":
    run()