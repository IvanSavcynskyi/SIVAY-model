# SIVAY: специфікація системи та розподіл роботи між агентами

**Що робить система.** Для поля рекомендує до 5 культур на наступний сезон. Для кожної культури пояснює, чому вона в списку, і показує обмеження та якість даних.

**Стек:** Python 3.12, FastAPI, Pydantic v2, PostgreSQL, локальні растрові файли (COG). Один сервер без GPU.

Детальне обґрунтування рішень: [`design/crop-recommendation-design.md`](design/crop-recommendation-design.md). Далі в тексті посилання виду «дизайн §N» ведуть на розділи цього документа.

---

## 1. Потік даних

```
[1] Вхід від користувача ──┐
[2] Готові таблиці цін ────┼──► PREPARE (збір і підготовка даних, асинхронно) ──► Snapshot поля
[3] Дані, які збираємо ────┘                                                         │
                                                                                     ▼
                                    RANK (чиста функція, без мережі) ──► [4] Результат
```

---

## 2. Вхід від користувача

`POST /v1/recommendations`. Поля з позначкою `*` обов'язкові.

```json
{
  "field": {
    "field_id": "fld_7f3a",                                   // *
    "geometry": {"type": "Polygon", "coordinates": [[[34.501,49.612],[34.523,49.612],[34.523,49.598],[34.501,49.598],[34.501,49.612]]]},  // * Polygon | MultiPolygon
    "crs": "EPSG:4326"                                        // * явно, без значення за замовчуванням
  },
  "as_of_date": "2026-09-01",                                 // * дата, на яку формується рекомендація; дані після неї не використовуються
  "target_season": {"harvest_year": 2027, "include": ["winter","spring"]},   // *
  "crop_history": [                                           // бажано 3–5 років
    {"season_year": 2025, "crop": "soybean", "variant": null, "role": "main",
     "sowing_date": "2025-04-28", "harvest_date": "2025-09-20",
     "yield_t_ha": 2.9, "source": "farm_records", "reliability": "high"},
    {"season_year": 2026, "crop": "sunflower", "role": "main", "status": "in_field",
     "harvest_date_planned": {"from": "2026-09-20", "to": "2026-10-05"},
     "source": "farm_records", "reliability": "high"}
  ],
  "history_gaps": [{"season_year": 2021, "reason": "unknown"}],
  "management": {"input_level": "high", "irrigation": "none"},       // якщо не вказано: high / none з прапорцем «припущення»
  "constraints": {"excluded_crops": ["sugar_beet"], "notes": null},
  "economics": {                                              // необов'язково; без цього блоку режим AGRO_ONLY
    "currency": "UAH",
    "price_basis": {"incoterm": "EXW", "vat": "excluded"},
    "costs_per_ha": {"wheat_winter": {"value": 30000, "drying_per_t": 0, "logistics_per_t": 0}},
    "expected_yield_t_ha": {"wheat_winter": {"low": 4.5, "base": 5.5, "high": 6.3}},
    "contract_prices": [{"crop": "wheat_winter", "price": 8800, "unit": "t", "basis": "EXW"}]
  },
  "options": {"max_results": 5, "language": "uk"}
}
```

**Правила для історії культур:**
- `role` приймає значення `main`, `second_crop` або `cover_crop`. Кілька записів за один рік дозволені.
- `source` приймає значення `farm_records`, `user_memory`, `platform_detected` або `imported`.
- `reliability` приймає значення `high`, `medium` або `low`.
- Дати можна подавати з різною точністю (`*_precision`: `day`, `month` або `season`).
- Якщо даних немає, поле має бути `null` або відсутнє. Значення `0` для позначення пропуску не допускається.

---

## 3. Дані, які надходять готовими

### 3.1 Таблиця ринкових цін (CSV або таблиця в БД)

| Колонка | Обов. | Приклад |
|---|---|---|
| `price_id` | ✔ | `p_88213` |
| `crop`, `commodity_spec` | ✔ | `wheat`, `grain_class3` |
| `price_value`, `currency`, `unit` | ✔ | `8500`, `UAH`, `t` |
| `price_date`, `published_at` | ✔ | `2026-08-28`, `2026-08-29T06:00Z` |
| `location_type`, `region` | ✔ | `elevator`, `UA-53` |
| `incoterm` | ✔ | `EXW` / `FCA` / `CPT` / `FOB` |
| `vat_included`, `vat_rate` | ✔ | `false`, `null` |
| `delivery_period` |  | `spot` / `2027-07` |
| `quality_basis` |  | `moisture 14%, protein 11.5%` |
| `price_type` |  | `bid` |
| `source`, `source_reliability` | ✔ | `apk-inform`, `medium` |

Окремо потрібна **таблиця курсів валют** з колонками `date`, `currency`, `rate_to_UAH`, `source`.

### 3.2 Супутникові індекси (API платформи, вже обчислені)

`GET {platform}/api/fields/{field_id}/indices?index=NDVI&from=&to=`

Для кожного спостереження потрібні:
- `date`, `acquired_at`, `available_at`;
- `valid_pixel_fraction`;
- статистики `mean`, `median`, `p10`, `p90`, `std`.

Для всього ряду потрібні `sensor`, `native_resolution_m` і `processing.version`.

Індекси: NDVI обов'язковий, NDMI бажаний.

---

## 4. Дані, які система збирає сама

| Джерело | Що беремо | Звідки | Для чого |
|---|---|---|---|
| **GAEZ v5, RES05** | `SUX` (основний шар), `SCX`, `SXX`, `SX1`, `YCX`, `WDC`; варіанти `30AS` для `SXX`, `SCX`, `YCX` | `https://storage.googleapis.com/fao-gismgr-gaez-v5-data/DATA/GAEZ-V5/MAPSET/<VAR>/GAEZ-V5.<VAR>.<PERIOD>.<CLIM>.<SSP>.<CROP>.<WSIM>.tif` | Агропридатність культури |
| GAEZ: сценарії | Основний `HP0120.AGERA5.HIST`; для перевірки чутливості `FP2140.ENSEMBLE.{SSP126,SSP370,SSP585}` | те саме | Інтервал оцінки, стійкість |
| GAEZ: довідники | `DIMENSION/GAEZ-V5.{CROP-RES05,PERIOD,CLIM,SSP,WSIM-RES05}.csv` | той самий бакет | Маніфест і коди |
| **Погода** | Добові `tmin`, `tmax`, `precip`, `et0`, `soil_moisture_0_7`, `soil_moisture_7_28`, `snow_depth`; норми за 1991–2020; поточний стан; прогноз до 16 днів | Прототип: Open-Meteo. Історія для production: AgERA5 / ERA5-Land з Copernicus CDS. Обидва варіанти ще треба перевірити (дизайн §9.2) | Аномалія умов посіву, ризики |
| Регіон | Код області за геометрією поля | Локальний шар адмінмеж | Посівні вікна, правила |
| **Агроправила** (готує агроном) | Каталог варіантів культур, посівні вікна, правила сівозміни, множники | `rules/` (YAML/CSV у git) | Бар'єри, `M_rot` |

**Що відомо про растри GAEZ** (перевірено):
- система координат EPSG:4326, крок 1/12°, формат COG;
- `SXX` і `SUX` мають тип int16, діапазон 0–10000, NoData = −9;
- `SIX` і `SCX` мають тип uint8, класи 1–9, NoData = 0; клас 8 означає «непридатна», клас 9 — вода;
- урожайність (`Y*`) зберігається як float32 у кг сухої речовини на гектар, NoData = −9;
- ліцензія — CC BY 4.0.

**Важливе обмеження.** У RES05 пшениця, ячмінь, ріпак і жито мають по одному коду (`WHE`, `BRL`, `RSD`, `RYE`), без поділу на озимі та ярі. Тому для озимих і ярих варіантів у системі ставимо позначку `gaez_match: parent_crop` (дизайн §4.1).

---

## 5. Розрахунок

Короткий алгоритм. Деталі формул — у дизайні §5.

1. **Кандидати.** Беремо варіанти культур з каталогу регіону, якщо для них є код GAEZ і посівне вікно перетинається із сезоном.
2. **Бар'єри.** Культуру виключаємо, якщо спрацьовує хоча б один із бар'єрів:
   - G1 — обмеження користувача;
   - G2 — посівне вікно або дата збирання попередника;
   - G3 — затверджене правило несумісності за надійної історії;
   - G4 — клас 8 у GAEZ на обох роздільностях, без суперечності з історією поля;
   - G5 — покриття GAEZ менше 0,5: культура отримує статус «не оцінено».
3. **Агрономічна оцінка як інтервал:** `Agro[lo,mid,hi] = A_gaez × M_rot × M_wx`. Пропуск даних розширює інтервал.
4. **Яруси.** Культури, чиї інтервали перекриваються з лідером, вважаються рівноцінними. Нижчий ярус ніколи не стоїть вище за вищий.
5. **Економіка.** Діє лише всередині ярусу і лише між культурами з повними даними (ціна, урожайність, витрати). Культури сортуються за сценарною маржею, решта зберігає свої місця.
6. **Стійкість.** Рейтинг перераховуємо на сітці збурень і рахуємо `p_top5` та діапазон місць.
7. **Відбір і пояснення.** Обираємо не більше 5 культур. Пояснення будуємо з шаблонів за подіями журналу розрахунку (trace).

---

## 6. Результат (контракт повернення)

```json
{
  "recommendation_id": "rec_01J9...",
  "status": "ok",                       // ok | needs_input | abstain | error
  "mode": "FULL",                       // FULL | AGRO_ONLY | LIMITED | NEEDS_INPUT | ABSTAIN
  "as_of_date": "2026-09-01",
  "computed_at": "2026-09-01T08:14:03Z",
  "results": [
    {
      "rank": 1,
      "crop_variant": "wheat_winter",
      "name_uk": "Пшениця озима",
      "tier": 1,
      "status": "recommended",          // recommended | conditional
      "agro_score": {"lo": 0.54, "mid": 0.63, "hi": 0.68},
      "gaez": {
        "layer": "GAEZ-V5.RES05-SUX.HP0120.AGERA5.HIST.WHE.HRLM",
        "crop_code": "WHE", "match": "parent_crop",
        "scenario": {"period": "HP0120", "clim": "AGERA5", "wsim": "HRLM"},
        "suitability_index": {"lo": 71, "mid": 78, "hi": 80},
        "class_shares": {"2": 0.83, "3": 0.17},
        "coverage_valid": 1.0, "resolution_deg": 0.0833, "field_to_cell_ratio": 0.021
      },
      "rotation": {"multiplier": {"lo": 0.9, "mid": 0.9, "hi": 0.9},
                   "rules_fired": [{"id": "ROT-PRED-SUNFLOWER-FOR-WINTER-CEREALS", "version": 1, "effect": "discouraged_mild"}]},
      "weather": {"multiplier": {"lo": 0.85, "mid": 0.9, "hi": 0.95}, "signal": "dry",
                  "risks": [{"code": "ESTABLISHMENT", "level": "high"}, {"code": "WINTERKILL", "level": "moderate"}]},
      "satellite": {"role": "history_check", "effect_on_rank": "none", "notes": ["record 2023 consistent with summer crop"]},
      "economics": {
        "applied_to_rank": true,
        "price": {"base": 8500, "currency": "UAH", "unit": "t", "basis": "EXW", "vat": "excluded", "price_date": "2026-08-28", "age_days": 4},
        "yield_t_ha": {"low": 4.5, "base": 5.5, "high": 6.3, "source": "user"},
        "revenue_uah_ha": {"low": 32512, "base": 46750, "high": 61583},
        "costs_uah_ha": 30000,
        "margin_uah_ha": {"low": 2512, "base": 16750, "high": 31583}
      },
      "stability": {"p_top5": 0.69, "rank_range": [1, 6], "label": "sensitive", "main_driver": "weather_multiplier"},
      "data_quality": {"level": "medium", "issues": ["history_2023_user_memory"]},
      "assumptions": ["input_level=high (user)", "GAEZ не розрізняє озиму/яру пшеницю"],
      "reasons": [{"text": "Придатність за GAEZ v5 ...", "trace_event_id": "ev_12"}],
      "limitations": [{"text": "Оцінка стосується комірки ~6×9 км ...", "trace_event_id": "ev_13"}]
    }
  ],
  "excluded": [{"crop_variant": "rapeseed_winter", "gate": "SOWING_WINDOW",
                "text": "Збирання попередника (20.09–05.10) пізніше за кінець вікна (05.09)", "trace_event_id": "ev_4"}],
  "below_threshold": [],
  "not_evaluated": [{"crop_variant": "mustard", "reason": "NOT_IN_GAEZ_CATALOG"}],
  "requests_for_input": [],             // наприклад {"field": "management.input_level", "why": "топ-5 змінюється"}
  "data_quality": {"overall": "medium", "by_source": {"gaez": "high", "history": "medium", "weather": "high", "satellite": "medium", "prices": "high", "costs": "medium"}},
  "provenance": {
    "algorithm_version": "rank-1.0.0", "rules_version": "rules-ua-2026.08+3e1c",
    "snapshot_id": "snap_...", "gaez_layers": [{"code": "...", "sha256": "..."}],
    "weather": [{"provider": "...", "kind": "reanalysis", "available_until": "2026-08-31"}],
    "price_ids": ["p_88213"], "robustness_grid_hash": "..."
  }
}
```

**Інваріанти результату:**
- Кількість рекомендацій від 0 до 5. Якщо обґрунтованих культур лише 3, повертаємо 3.
- `agro_score` не є ймовірністю. Маржа не є прибутком.
- Кожне речення в `reasons` і `limitations` має `trace_event_id`.
- Повторний розрахунок з тими самими `snapshot_id` і `rules_version` дає ідентичний результат.

**Допоміжні endpoints:**

| Метод і шлях | Призначення |
|---|---|
| `POST /v1/fields/{id}/prepare` | Повертає 202 і `job_id` |
| `GET /v1/jobs/{id}` | Статус задачі підготовки |
| `GET /v1/recommendations/{id}` | Збережений результат |
| `GET /v1/catalog/crops?region=` | Каталог культур регіону |
| `GET /v1/rules/versions` | Версії правил |
| `GET /v1/health` | Стан сервісу |

---

## 7. Структура репозиторію

```
app/
  domain/        # Pydantic-моделі: контракти §2, §3, §6 — спільні для всіх агентів
  api/           # FastAPI-роутери
  adapters/      # gaez_store, weather, prices, satellite, platform
  geo/           # валідація геометрії, extract (exactextract)
  features/      # gaez_features, rotation, weather_features, satellite_features, economics
  ranking/       # candidates, gates, scoring, tiers, robustness, select
  explain/       # шаблони uk, рендер за trace
  storage/       # БД, кеш, snapshot, jobs
rules/           # catalog.yaml, sowing_windows/*.csv, rotation/*.yaml, multipliers.yaml
data/gaez/       # COG + manifest (не в git)
tests/           # unit, property, golden, leakage
docs/
```

---

## 8. Ролі агентів

**Загальні правила для всіх агентів:**
- Контракти в `app/domain/` змінює лише A0. Решта агентів пропонує зміни через нього.
- Не вигадуйте агрономічних чисел, URL і кодів. Невідоме значення залишайте як `null` зі статусом `draft` або позначкою «НЕ ПЕРЕВІРЕНО».
- Пропуск даних — це `None`, а не `0`. Пропуск ніколи не має покращувати позицію культури.
- Кожен адаптер фільтрує дані за умовою `available_at ≤ as_of`.
- Модуль `ranking/` не звертається до мережі.

| Агент | Відповідає за | Вхід | Вихід / готово, коли |
|---|---|---|---|
| **A0. Архітектор-інтегратор** | Каркас репозиторію, `app/domain` (усі Pydantic-контракти з §2, §3, §6), конфіг, БД-схема, CI, збирання модулів у конвеєр `prepare` → `rank` | Цей файл, дизайн §7, §11 | Контракти замкнені, CI зелений, наскрізний виклик проходить на заглушках |
| **A1. GAEZ і геообробка** | Імпорт маніфесту з бакета (лістинг + `DIMENSION/*.csv`), завантаження потрібних COG (30″ обрізати до країни), перевірка dtype і NoData; `geo/`: валідація геометрії й CRS, `exactextract`, числові агрегати з вагами площі, частки класів (без усереднення кодів), `coverage_valid`, кеш за `geom_hash` | Дизайн §4.1, §6 | `GaezFieldValue` для будь-якого поля; тести на синтетичних растрах; звірка 20 точок з порталом GAEZ. З'ясувати семантику `WHE`/`HWHE`/`SWHE` і `RES02-IDX` та зафіксувати висновок |
| **A2. Агроправила** (разом з агрономом-людиною) | `rules/catalog.yaml` (варіанти, відповідність кодам GAEZ, родини, `establishment_moisture_sensitive`), `sowing_windows/*.csv`, `rotation/*.yaml`, `multipliers.yaml`; схеми й валідатор; таблиця-оракул пар «попередник → кандидат» | Дизайн §5.3, §5.5.2, §7.6 | Правила проходять валідацію; кожне `approved` правило має джерело і `verified_by`; оракул покриває 30–50 пар |
| **A3. Погода** | Адаптер `WeatherProvider.get(kind, lat, lon, vars, period, as_of)`; норми 1991–2020; перцентилі вологості ґрунту й опадів; прогноз до 16 днів; кліматологічні ризики (заморозок, зимівля, спека, сухі періоди); кеш | Дизайн §5.5.3, §9.1–9.2 | `WeatherFeatures` з `available_at`; тест на витік даних з майбутнього; перевірені endpoints і ліцензія провайдера |
| **A4. Історія і супутник** | Нормалізація історії (коди, сезони, кілька культур за рік, пропуски, надійність); QC індексів; перевірка фенології (озима / яра / пізня літня) лише для коригування `reliability`; прапорець «поле зайняте» | Дизайн §7.2, §9.3 | `NormalizedHistory` і `SatelliteFeatures`; супутник не змінює рейтинг напряму |
| **A5. Економіка** | Завантажувач таблиці цін (§3.1), нормалізація (одиниці, валюта, ПДВ, основа поставки), свіжість, сценарії low/base/high, урожайність (користувач, потім статистика), витрати, маржа | Дизайн §10 | `EconomicsResult` на культуру з ланцюжком перетворень; без витрат — лише виручка з `applied_to_rank=false` |
| **A6. Рушій ранжування** | `ranking/`: кандидати, бар'єри G1–G5, інтервали `A`, `M_rot`, `M_wx`, поріг, яруси, перестановка за економікою всередині ярусу, нічиї, сітка стійкості, відбір ≤ 5, режими `FULL` / `AGRO_ONLY` / `LIMITED` / `NEEDS_INPUT` / `ABSTAIN`, trace-події | Дизайн §5, §9.4, §11 | Чиста детермінована функція; property-тести на монотонність і «пропуск не покращує»; golden-тест на прикладі з дизайну §8 |
| **A7. Пояснення, API, зберігання** | `explain/` (шаблони uk за кодами trace), FastAPI-endpoints (§6), асинхронні задачі (PostgreSQL + worker), snapshot, збереження результатів і provenance, обробка помилок зовнішніх джерел (retry, кеш, `partial`) | Дизайн §5.11, §7 | Відповідь точно відповідає контракту §6; кожне речення має `trace_event_id`; повторний розрахунок відтворює результат |
| **A8. Якість і валідація** | Фікстури полів, golden-сценарії, тести на всі рядки таблиці пропусків (дизайн §9.4), канарейка на витік даних, бектест-режим, абляція C0 → C3, пакет для сліпої експертизи агрономом | Дизайн §12 | Звіт про якість; список параметрів для калібрування |

### Порядок і залежності

```
Крок 1:  A0 (контракти, каркас)
Крок 2:  A1 ║ A2 ║ A5 ║ A4 ║ A3          — паралельно, на контрактах A0
Крок 3:  A6 (спершу на A1 + A2 → прототип AGRO_ONLY, потім підключає A3, A4, A5)
Крок 4:  A7 (API, пояснення, зберігання)
Наскрізно: A8 пише тести з кроку 2; фінальна валідація після кроку 4
```

**Етапи готовності:**

| Етап | Що має працювати |
|---|---|
| **M1. Прототип** | A0 + A1 + A2 + A6 + мінімальний A7: режим `AGRO_ONLY` для одного регіону |
| **M2. Функціональна версія** | Додано A3, A4, A5; стійкість, режими пропусків, provenance |
| **M3. Готова система** | Тести A8 зелені; експертна перевірка пройдена; параметри зі статусом `draft` або затверджені, або вимкнені |

---

## 9. Готовність системи (Definition of Done)

- [ ] Вхід з §2 валідується. Невалідна геометрія або відсутня `as_of_date` чи `target_season` дають зрозумілу помилку.
- [ ] Для поля в підтримуваному регіоні повертається відповідь за контрактом §6 у будь-якому з 5 режимів.
- [ ] Висока ціна не піднімає культуру з нижчого агроярусу. Пропуск даних не покращує місце культури.
- [ ] Немає витоку даних з майбутнього (тести A8).
- [ ] Результат відтворюється з `snapshot_id` і `rules_version`.
- [ ] Усі пояснення прив'язані до trace. Немає слів «прибуток» і «ймовірність успіху».
- [ ] Правила та множники, які впливають на рейтинг, мають статус `approved` і джерело.
