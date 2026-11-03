# Розгортання на EC2 ARM64

Стек: Python 3.12/FastAPI, PostgreSQL 16, один worker. Docker Compose запускає
сервіси після перезавантаження, обмежує пам'ять і розмір журналів. API слухає
`127.0.0.1:8000` сервера. Спільний Bearer-ключ призначений для довіреного
бекенду, а не для розповсюдження у браузерному фронтенді.

## Підготовка

У корінь `/home/ubuntu/sivay` потрібно перенести поточні `app`, `rules`,
`scripts`, `examples`, `Dockerfile`, `compose.yaml`, `pyproject.toml`,
`requirements.lock` і `.dockerignore`. Перенесення через Git не включає
незакомічені файли: перед випуском перевірте склад версії.

Окремо потрібні `data/gaez` з manifest і GeoTIFF та `data/regions.geojson`.
Їх можна перенести з перевіреного локального набору або завантажити імпортером
`python scripts/import_mvp_data.py`. Дані монтуються в контейнери тільки для
читання. Приватні `.env`, SQLite-БД, SSH-ключі й `.git` не переносіть у Docker-образ.

На Ubuntu виконайте `bash scripts/provision_ec2.sh`: він встановлює Docker,
вмикає його автозапуск і на машині без swap створює 2 GiB swap як запас пам'яті.
Це використовує місце на наявному диску, окремий EBS volume не створюється. Далі:

```bash
cd /home/ubuntu/sivay
python3 scripts/init_deploy_env.py
sudo docker compose build api
sudo docker compose up -d --no-build
sudo docker compose ps
sudo docker compose exec -T api python -m app.healthcheck
```

Генератор створює `.env` з правами `0600` і випадковими паролем PostgreSQL та
API-ключем; існуючий файл не перезаписує. Docker Compose передає налаштування
обом Python-процесам. `SIVAY_JOB_LEASE_SECONDS=900` залишає час на мережеві
джерела. Для нестандартних паролів БД символи в URL потребують percent-encoding;
генератор використовує hex без спеціальних символів.

## Підключення з Mac

У новому локальному терміналі відкрийте тунель і залиште його працювати:

```bash
ssh -i ~/Downloads/sivay.pem -N -L 8000:127.0.0.1:8000 ubuntu@13.53.151.170
```

Swagger: <http://localhost:8000/docs>. Щоб отримати ключ, у своїй SSH-сесії
на сервері виконайте `grep '^SIVAY_API_KEY=' /home/ubuntu/sivay/.env`.
У Swagger натисніть **Authorize**, вставте тільки значення після `=`,
без префікса `Bearer`. У HTTP-клієнті передавайте `Authorization: Bearer <ключ>`.
Для тунелю не потрібно відкривати порт 8000 у Security Group.

Для публічного доступу потрібні домен, DNS на сервер і reverse proxy з HTTPS.
Сам API та PostgreSQL залишаються за proxy; Bearer-ключ передавайте лише через
HTTPS або SSH-тунель. Якщо DNS посилається на автоматичний public IP EC2,
після stop/start адреса може змінитися.

## Запит і джерела

Використовуйте `examples/request.json`, змінивши геометрію, історію культур,
цільовий сезон і `as_of_date` на поточну дату UTC. Для порівняння культур
економіку не передавайте і не імпортуйте ціни/витрати. Поле `economics` збережено
для сумісності контракту; без економічних даних його значення залишаються `null`.

Для повільної підготовки бажаний порядок:

1. `POST /v1/fields/{field_id}/prepare` з повним тілом запиту.
2. `GET /v1/jobs/{job_id}` до `completed`, `partial` або `failed`.
3. `POST /v1/recommendations` з тим самим тілом — зберігає підсумковий результат.
4. `GET /v1/recommendations/{recommendation_id}/analysis` — фактичні метрики,
   дати, погодні джерела, супутникові сцени, GAEZ та внески правил.

Підготовка отримує історичну погоду та конкретний прогнозний запуск Open-Meteo,
шукає Sentinel-2 L2A через Earth Search, читає вікна каналів за полігоном,
маскує хмари й обчислює NDVI/NDMI. Повні супутникові тайли не завантажуються;
зберігаються індекси та метадані сцен, а не оригінальні багатоканальні файли.
GAEZ читається з локальних перевірених растрів. Кеш: погода 1 година,
супутникові дані 6 годин. Повторення за `snapshot_id` не потребує мережі.

Нові мережеві дані не підтягуються для історичної `as_of_date`. Для числового
впливу поточної погоди та індексів потрібні дати посіву в
`personalization.sowing_dates`; далекі строки посіву не оцінюються за сьогоднішнім
коротким прогнозом. Статус `limited` означає, що правила ще потребують польової
та агрономічної перевірки. Збій зовнішнього джерела повертається в аналізі й
попередженнях, а не підмінюється вигаданими значеннями.

Free API Open-Meteo підходить для некомерційного тестування; комерційна
експлуатація потребує окремого налаштування відповідного тарифу/провайдера:
<https://open-meteo.com/en/pricing>.

Наскрізна перевірка з реальними джерелами:

```bash
sudo docker compose exec -T api python scripts/smoke_deployment.py --require-sources
```

Цей запит використовує приклад поля та історії; це не фактична декларація ферми.
`--synthetic-near-sowing` окремо перевіряє поправки на умовному близькому посіві;
результат явно позначає цей план синтетичним. JSON-звіт записується у
`/tmp/sivay-http-smoke.json` контейнера; ключ до нього не входить.

## Обслуговування

```bash
sudo docker compose logs --tail=100 api worker
sudo docker stats --no-stream
sudo bash scripts/backup_postgres.sh
```

Щоденну копію можна ввімкнути через надані systemd unit-файли:

```bash
sudo cp deploy/sivay-backup.service deploy/sivay-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sivay-backup.timer
```

Копії накопичуються в `backups/`; контролюйте вільне місце і переносьте старі
копії на окремий носій.

Резервні копії створюються в `backups/` з доступом лише власнику. Копіюйте їх
на окремий носій: копія на тому самому сервері не захищає від втрати диска.
Для відновлення у **нову порожню** базу використовуйте
`docker compose exec -T postgres pg_restore -U sivay -d sivay < backups/FILE.dump`.
Не виконуйте `docker compose down -v`: це видалить дані PostgreSQL.

Для оновлення збережіть `.env`, `data/` і Docker volume, перенесіть нову версію,
виконайте `docker compose build api`, потім `docker compose up -d --no-build`.
Після зміни схеми БД спочатку потрібна окрема перевірка сумісності/міграція.
