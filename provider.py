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
        print("Firebase ініціалізовано.")
    else:
        print("ПОМИЛКА: Змінна FIREBASE_JSON не знайдена!")

# Flask-сервер (потрібен для Render)
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
    print("Колектор даних запущено!")
    ref_vehicles = db.reference('vehicles') 
    ref_alerts = db.reference('alerts')
    
    while True:
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Спроба отримати дані з track.ua-gis.com...")
            
            response = requests.get(REALTIME_URL, timeout=20)
            
            if response.status_code == 200:
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(response.content)
                
                if len(feed.entity) == 0:
                    print("Отримано порожню відповідь від сервера міста.")
                else:
                    print(f"Отримано {len(feed.entity)} ТЗ. Обробка...")
                    mapping = load_mapping()
                    routes_map = get_routes_map()
                    payload = {}
                    
                    for entity in feed.entity:
                        if entity.HasField('vehicle'):
                            v = entity.vehicle
                            raw_id = v.vehicle.id
                            name = routes_map.get(str(v.trip.route_id), "Unknown")
                            vehicle_data = {
                                'lat': v.position.latitude,
                                'lng': v.position.longitude,
                                'r': name,
                                'type': v.vehicle.label,
                                'p': mapping.get(raw_id, [raw_id])[0] if raw_id in mapping else raw_id,
                                'b': float(v.position.bearing) if v.position.HasField('bearing') else 0.0,
                                's': v.position.speed if v.position.HasField('speed') else 0.0,
                                'last_updated': int(time.time())
                            }
                            payload[raw_id] = vehicle_data
                    
                    ref_vehicles.update(payload)
                    print(f"Успішно оновлено {len(payload)} ТЗ у Firebase.")
            else:
                print(f"Помилка сервера міста: статус {response.status_code}")
                
        except Exception as e:
            print(f"КРИТИЧНА ПОМИЛКА в циклі: {e}")
            
        time.sleep(30) # Пауза 30 секунд

# Запуск колектора у фоновому потоці
threading.Thread(target=run_collector, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)