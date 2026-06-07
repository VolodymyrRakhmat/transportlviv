import requests
import zipfile
import time
import json
import firebase_admin
import os
import threading
from io import BytesIO
from firebase_admin import credentials, db
from datetime import datetime
from google.transit import gtfs_realtime_pb2
import pandas as pd
from flask import Flask

# --- Налаштування ---
DB_URL = 'https://lviv-transport-web-default-rtdb.firebaseio.com/'
STATIC_URL = "http://track.ua-gis.com/gtfs/lviv/static.zip"
REALTIME_URL = "http://track.ua-gis.com/gtfs/lviv/vehicle_position"
MAPPING_FILE = "public/mapping.json"

# --- Ініціалізація Firebase ---
if not firebase_admin._apps:
    json_env = os.environ.get('FIREBASE_JSON')
    if json_env:
        firebase_config = json.loads(json_env)
        cred = credentials.Certificate(firebase_config)
        firebase_admin.initialize_app(cred, {'databaseURL': DB_URL})
    else:
        print("ПОМИЛКА: Змінна FIREBASE_JSON не знайдена!")

# Flask-сервер
app = Flask(__name__)

@app.route('/')
def home():
    return "Моніторинг працює!"

def get_routes_map():
    try:
        r = requests.get(STATIC_URL, timeout=30)
        z = zipfile.ZipFile(BytesIO(r.content))
        with z.open('routes.txt') as f:
            routes = pd.read_csv(f)
            return {str(row['route_id']).strip(): str(row['route_short_name']).strip() for _, row in routes.iterrows()}
    except Exception as e:
        print(f"Помилка завантаження маршрутів: {e}")
        return {}

def load_mapping():
    try:
        if os.path.exists(MAPPING_FILE):
            with open(MAPPING_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}
    except Exception as e:
        print(f"Помилка завантаження mapping.json: {e}")
        return {}

def run_collector():
    # Записуємо дані у гілку 'vehicles', як того хоче сайт
    ref_vehicles = db.reference('vehicles') 
    ref_alerts = db.reference('alerts')
    ref_history = db.reference('history')
    
    last_history_save = 0
    HISTORY_INTERVAL = 300 
    
    print("Колектор даних запущено!")
    while True:
        try:
            mapping = load_mapping()
            routes_map = get_routes_map()
            
            feed = gtfs_realtime_pb2.FeedMessage()
            response = requests.get(REALTIME_URL, timeout=15)
            feed.ParseFromString(response.content)
            
            payload = {}
            alerts_payload = {}
            history_snapshot = {}
            current_ts = time.time()
            current_date = datetime.now().strftime('%Y-%m-%d')
            current_time_str = datetime.now().strftime('%H:%M')

            for entity in feed.entity:
                if entity.HasField('vehicle'):
                    v = entity.vehicle
                    raw_id = v.vehicle.id
                    name = routes_map.get(str(v.trip.route_id), "Unknown")
                    
                    is_identified = raw_id in mapping
                    display_val = mapping[raw_id][0] if is_identified else raw_id
                    
                    vehicle_data = {
                        'lat': v.position.latitude,
                        'lng': v.position.longitude,
                        'r': name,
                        'type': v.vehicle.label,
                        'p': display_val,
                        'raw_p': raw_id,
                        'b': float(v.position.bearing) if v.position.HasField('bearing') else 0.0,
                        's': v.position.speed if v.position.HasField('speed') else 0.0,
                        'timestamp': current_time_str,
                        'updated_at': datetime.now().isoformat()
                    }

                    payload[raw_id] = vehicle_data
                    if not is_identified:
                        alerts_payload[raw_id] = {'id': raw_id, 'route': name, 'time': current_time_str}
                    if current_ts - last_history_save >= HISTORY_INTERVAL:
                        history_snapshot[raw_id] = vehicle_data

            if payload:
                ref_vehicles.update(payload)
            ref_alerts.set(alerts_payload)
            
            if history_snapshot:
                history_key = current_time_str.replace(':', '-')
                ref_history.child(current_date).child(history_key).set(history_snapshot)
                last_history_save = current_ts

            print(f"[{current_time_str}] Оновлено {len(payload)} ТЗ")
            time.sleep(11)

        except Exception as e:
            print(f"Помилка циклу: {e}")
            time.sleep(60)

# --- ЗАПУСК ---
# Запускаємо збір даних у фоновому потоці ОДРАЗУ
threading.Thread(target=run_collector, daemon=True).start()