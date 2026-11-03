# Контракт рекомендацій

## Вхід — `POST /v1/recommendations`

| Поле | Обов’язкове | Формат / приклад | Пояснення |
|---|---|---|---|
| `field.field_id` | Так | `"field-001"` | Ідентифікатор поля |
| `field.geometry` | Так | `{"type":"Polygon","coordinates":[...]}` | Геометрія `Polygon` або `MultiPolygon`; для EPSG:4326 координати `[довгота, широта]` |
| `field.crs` | Так | `"EPSG:4326"` | Система координат |
| `as_of_date` | Так | `"2026-10-21"` | Дата оцінки; використовуються дані, доступні до кінця цього дня UTC |
| `target_season.harvest_year` | Так | `2027` | Рік урожаю |
| `target_season.include` | Так | `["spring"]` | Непорожній список: `winter`, `spring`, `summer`; `spring` включає також літні культури |
| `crop_history` | Ні | `[{"crop":"soybean","year":2025}]` | Історія від бекенду; порожній або пропущений список означає невідому історію, з БД не підтягується |
| `crop_history[].crop` | Так, у записі | `"soybean"` | Ідентифікатор попередньої культури |
| `crop_history[].year` | Так, у записі | `2025` | Рік урожаю; також приймається назва `season_year` |
| `history_gaps` | Ні | `[{"season_year":2024,"reason":"unknown"}]` | Прогалини історії |
| `management.input_level` | Ні | `"high"` / `"low"` | Рівень агротехнологій; типово `high` |
| `management.irrigation` | Ні | `"none"` / `"irrigated"` | Зрошення; типово `none` |
| `constraints` | Ні | `{"excluded_crops":["sunflower"]}` | Виключені культури та примітки |
| `economics` | Ні | Об’єкт | Витрати, сценарії урожайності й контрактні ціни для розрахунку економіки |
| `personalization` | Ні | `{"sowing_dates":{"wheat_spring":"2027-04-10"}}` | Дати посіву, правила, NDVI/NDMI та погода |
| `options.max_results` | Ні | `5` | Кількість результатів: 1–5; типово 5 |
| `options.language` | Ні | `"uk"` | Мова відповіді; підтримується лише `uk` |

Приклад запиту:

```json
{
  "field": {
    "field_id": "poltava_example",
    "geometry": {
      "type": "Polygon",
      "coordinates": [[[34.501,49.612],[34.523,49.612],[34.523,49.598],[34.501,49.598],[34.501,49.612]]]
    },
    "crs": "EPSG:4326"
  },
  "as_of_date": "2026-10-21",
  "target_season": {"harvest_year": 2027, "include": ["spring"]},
  "crop_history": [
    {"crop": "soybean", "year": 2025},
    {"crop": "wheat_winter", "year": 2026}
  ],
  "management": {"input_level": "high", "irrigation": "none"},
  "options": {"max_results": 5, "language": "uk"}
}
```

## Відповідь

| Поле | Обов’язкове | Формат | Пояснення |
|---|---|---|---|
| `recommendation_id` | Так | Рядок | Ідентифікатор збереженої рекомендації |
| `snapshot_id` | Так | Рядок | Ідентифікатор даних для відтворення розрахунку |
| `status` | Так | Рядок | `ok`, `limited`, `needs_input`, `abstain` або `error` |
| `as_of_date` | Так | `YYYY-MM-DD` | Дата, на яку сформовано рекомендацію |
| `demo` | Так | `true` / `false` | Чи використано синтетичні дані |
| `results` | Так | Масив | Від 0 до 5 культур у порядку `rank` |
| `warnings` | Так | Масив рядків | Попередження й потрібні уточнення; може бути `[]` |
| `results[].rank` | Так, у результаті | Число 1–5 | Місце культури в рейтингу |
| `results[].crop_id` | Так, у результаті | Рядок | Ідентифікатор варіанта культури в каталозі |
| `results[].name_uk` | Так, у результаті | Рядок | Назва культури українською |
| `results[].score` | Так, у результаті | Число 0–1 | Центральна оцінка `mid`; не ймовірність урожаю; межі `lo/hi` не повертаються |
| `results[].score_explanation` | Так, у результаті | Рядок | Пояснення шкали, базової оцінки й застосованих поправок |
| `results[].economics` | Так, у результаті | Об’єкт | Економічні показники; невідомі значення — `null` |
| `results[].economics.price_uah_t` | Так | Число або `null` | Довідкова ціна, грн/т |
| `results[].economics.price_date` | Так | `YYYY-MM-DD` або `null` | Дата ціни |
| `results[].economics.yield_t_field` | Так | Число або `null` | Очікуваний урожай на все поле, т |
| `results[].economics.revenue_uah_field` | Так | Число або `null` | Виручка на все поле, грн |
| `results[].economics.costs_uah_field` | Так | Число або `null` | Надані витрати на все поле, грн |
| `results[].economics.margin_uah_field` | Так | Число або `null` | Маржа на все поле за наданими витратами, грн; може бути від’ємною |
