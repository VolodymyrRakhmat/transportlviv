import requests, zipfile, time, json, firebase_admin
from io import BytesIO
from firebase_admin import credentials, db
from datetime import datetime
from google.transit import gtfs_realtime_pb2
import pandas as pd
import os

DB_URL = 'https://lviv-transport-web-default-rtdb.firebaseio.com/'
STATIC_URL = "http://track.ua-gis.com/gtfs/lviv/static.zip"
REALTIME_URL = "http://track.ua-gis.com/gtfs/lviv/vehicle_position"
MAPPING_FILE = "public/mapping.json"

if not firebase_admin._apps:
    cred_json = json.loads(os.environ.get('FIREBASE_JSON'))
    firebase_admin.initialize_app(credentials.Certificate(cred_json), {'databaseURL': DB_URL})


def get_routes_map():
    try:
        r = requests.get(STATIC_URL, timeout=30)
        z = zipfile.ZipFile(BytesIO(r.content))
        with z.open('routes.txt') as f:
            routes = pd.read_csv(f)
            return {str(row['route_id']).strip(): str(row['route_short_name']).strip() for _, row in routes.iterrows()}
    except:
        return {}

def load_mapping():
    try:
        with open(MAPPING_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def run():
    routes_map = get_routes_map()
    ref_live = db.reference('vehicles')
    ref_alerts = db.reference('alerts')
    ref_history = db.reference('history')

    trolley_numbers = ['22', '23', '24', '25', '30', '31', '32', '33', '38']

    last_history_save = 0
    HISTORY_INTERVAL = 300
    last_cleanup = 0
    CLEANUP_INTERVAL = 30   # частіше

    while True:
        try:
            vehicle_mapping = load_mapping()
            res = requests.get(REALTIME_URL, timeout=(10, 60))
            
            if res.status_code == 200:
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(res.content)

                current_ts = int(time.time())
                current_date = datetime.now().strftime("%Y-%m-%d")
                current_time_str = datetime.now().strftime("%H:%M:%S")

                # ==================== АГРЕСИВНИЙ CLEANUP ====================
                if current_ts - last_cleanup >= CLEANUP_INTERVAL:
                    existing = ref_live.get() or {}
                    feed_ids = set()

                    for entity in feed.entity:
                        if entity.HasField('vehicle'):
                            v = entity.vehicle
                            raw_id = str(getattr(v.vehicle, 'id', '')).strip()
                            if raw_id:
                                feed_ids.add(raw_id)

                    to_delete = []
                    for veh_id, data in existing.items():
                        if veh_id not in feed_ids:
                            last_upd = int(data.get('last_updated', 0))
                            if current_ts - last_upd > 120:      # 2 хвилини
                                to_delete.append(veh_id)

                    for vid in to_delete:
                        ref_live.child(vid).delete()

                    if to_delete:
                        print(f"🧹 Видалено привидів: {len(to_delete)} | Залишилось: {len(existing)-len(to_delete)}")
                    last_cleanup = current_ts
                # ===========================================================

                payload = {}
                alerts_payload = {}
                history_snapshot = {}

                for entity in feed.entity:
                    if not entity.HasField('vehicle'):
                        continue

                    v = entity.vehicle
                    raw_id = str(getattr(v.vehicle, 'id', '')).strip()
                    if not raw_id: continue

                    r_id = str(v.trip.route_id).strip()
                    name = routes_map.get(r_id, r_id)

                    display_val = vehicle_mapping.get(raw_id, raw_id)
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

                    vehicle_data = {
                        'r': name,
                        'type': v_type,
                        'lat': current_lat,
                        'lng': current_lng,
                        'p': display_val,
                        'raw_p': raw_id,
                        'b': float(v.position.bearing) if v.position.HasField('bearing') else 0.0,
                        's': current_speed,
                        'stop': stop,
                        'has_bort': is_identified,
                        'timestamp': current_time_str,
                        'last_updated': gtfs_timestamp,
                        'updated_at': datetime.now().isoformat()
                    }

                    payload[raw_id] = vehicle_data

                    if not is_identified:
                        alerts_payload[raw_id] = {'id': raw_id, 'route': name, 'time': current_time_str}

                    if current_ts - last_history_save >= HISTORY_INTERVAL:
                        history_snapshot[raw_id] = vehicle_data

                if payload:
                    ref_live.update(payload)

                ref_alerts.set(alerts_payload)

                if history_snapshot:
                    history_key = current_time_str.replace(':', '-')
                    ref_history.child(current_date).child(history_key).set(history_snapshot)
                    last_history_save = current_ts

                print(f"[{current_time_str}] Оновлено {len(payload)} ТЗ")

            time.sleep(11) 

        except Exception as e:
            print(f"Помилка: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()