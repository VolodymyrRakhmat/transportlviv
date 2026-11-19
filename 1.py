import json
import os

def convert_all(input_file, output_line_file, output_stops_file):
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 1. Витягуємо лінію маршруту
    # Ваш файл - це FeatureCollection, лінія зазвичай у першому feature
    coords = data['features'][0]['geometry']['coordinates']
    # Розвертаємо [lon, lat] на [lat, lon]
    line = [[c[1], c[0]] for c in coords[0]] 

    # 2. Витягуємо зупинки (вони у вас у списку features далі)
    stops = []
    for feature in data['features']:
        if feature['geometry']['type'] == 'Point':
            stops.append({
                "id": feature['properties']['stop_id'],
                "name": feature['properties']['stop_name'],
                "lat": feature['geometry']['coordinates'][1],
                "lng": feature['geometry']['coordinates'][0]
            })

    if not os.path.exists('routes'): os.makedirs('routes')

    # Зберігаємо окремими файлами
    with open(output_line_file, 'w', encoding='utf-8') as f:
        json.dump(line, f)
    with open(output_stops_file, 'w', encoding='utf-8') as f:
        json.dump(stops, f, ensure_ascii=False, indent=2)

    print(f"Готово! Лінія: {len(line)} точок, зупинок: {len(stops)}")

# Запускаємо: беремо ваш файл, створюємо 80_line.json та 80_stops.json
convert_all('03.geojson', 'routes/Т03_line.json', 'routes/Т03_stops.json')