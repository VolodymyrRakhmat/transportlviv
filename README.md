# Lviv Transport — provider (GTFS realtime → Firebase)

Скрипт постійно опитує GTFS-realtime фід Львова, оновлює дані в Firebase
Realtime Database та шле email-сповіщення про невідомі борти й простій автопарку.

## Файли в цьому репозиторії

- `provider.py` — сам скрипт
- `requirements.txt` — Python-залежності
- `.gitignore` — щоб секрети (ключ Firebase, `.env`) не потрапили в git

## 1. Перед тим як заливати на GitHub

Оригінальний файл містив пароль від пошти прямо в коді — це небезпечно
показувати світу. Я переніс і пароль, і ключ Firebase у **змінні
середовища**, тому в самому коді тепер секретів немає — його можна
безпечно комітити.

`serviceAccountKey.json` (ключ сервісного акаунта Firebase) **не** кладіть
у репозиторій — `.gitignore` вже його виключає.

## 2. Заливка на GitHub

```bash
cd project
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<ваш-акаунт>/<назва-репо>.git
git push -u origin main
```

## 3. Деплой на Render

Це нескінченний цикл (`while True`), а не веб-сервер — він нічого не
слухає на HTTP-порту. Такому скрипту на Render відповідає тип сервісу
**Background Worker**, а не Web Service:

1. New → Background Worker → підключити цей GitHub-репозиторій.
2. Build Command: `pip install -r requirements.txt`
3. Start Command: `python provider.py`
4. У розділі Environment додати змінні:
   - `FIREBASE_CREDENTIALS_JSON` — весь вміст вашого `serviceAccountKey.json`,
     вставлений одним рядком (JSON) як значення змінної
   - `FIREBASE_DB_URL` — URL вашої Firebase RTDB (за замовчуванням уже
     прописаний lviv-transport, можна не задавати, якщо це той самий проєкт)
   - `EMAIL_FROM` — Gmail-адреса відправника
   - `EMAIL_TO` — куди слати сповіщення (можна не задавати — за замовчуванням
     дорівнює `EMAIL_FROM`)
   - `EMAIL_PASSWORD` — **App Password** Gmail (не звичайний пароль від
     акаунта; створюється в налаштуваннях безпеки Google)

⚠️ Важливо: Background Worker на Render — платний тип сервісу (немає
безкоштовного тарифу саме для воркерів; безкоштовний є тільки для Web
Service, і той засинає без HTTP-запитів). Якщо потрібен безкоштовний
варіант, є два шляхи:
- найдешевший платний план (Starter, ~$7/міс) для Background Worker — просто і надійно;
- або переробити скрипт так, щоб він піднімав маленький HTTP-сервер
  (наприклад на Flask) і крутив цикл опитування в окремому потоці — тоді
  можна деплоїти як безкоштовний Web Service, і зовнішнім пінгером
  (наприклад cron-job.org) будити його раз на 10-14 хвилин, щоб він не засинав.
  Це працює, але не так надійно, як платний Background Worker.

Скажіть, який варіант вам підходить — можу підготувати версію з Flask-обгорткою
під безкоштовний план, якщо треба.

## 4. Локальний запуск (для перевірки)

```bash
pip install -r requirements.txt
export EMAIL_FROM="you@gmail.com"
export EMAIL_PASSWORD="app-password"
# або покладіть serviceAccountKey.json поруч зі скриптом замість
# FIREBASE_CREDENTIALS_JSON — для локальної розробки це підтримується
python provider.py
```
