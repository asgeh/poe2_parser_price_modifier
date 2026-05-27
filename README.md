# POE2 Price Modifier Analyzer

Инструмент для анализа дорогих предметов в Path of Exile 2 через официальное Trade API.
Собирает выборку предметов по ценовым окнам, считает статистику по модификаторам и комбинациям,
строит финальный поисковый запрос с топовыми модами и сохраняет всё в Excel-отчёт.

Работает для любой категории предметов: jewel, accessory (амулеты/кольца/пояса), armour, weapon и т.д.

---

## Как работает пайплайн

### Шаг 1 — Загрузка stat map

Читается `stats.json` — дамп эндпоинта `GET /api/trade2/data/stats`.
Из групп `explicit`, `implicit`, `desecrated` строится словарь:

```
normalized_mod_text → [stat_id, ...]
```

Нормализация: числа заменяются на `#`, текст приводится к нижнему регистру.
Пример: `"+15 to Strength"` → `"+# to strength"` → `"explicit.stat_3299347043"`.

---

### Шаг 2 — Сбор кандидатов

Пайплайн делает несколько поисковых проходов по Trade API в рамках заданных **ценовых окон** (`--price-window`).

Для каждого окна и каждого прохода:

1. `POST /api/trade2/search/{realm}/{league}` — поиск по категории предмета, ценовому диапазону, только identified предметы.
   Возвращает список item ID (до ~100 шт.).

2. `GET /api/trade2/fetch/{ids}?query={search_id}` — получение данных по batch из 10 ID за раз.
   Из каждого предмета извлекаются:
   - цена в выбранной валюте
   - список модификаторов (`explicitMods`, `implicitMods`, `desecratedMods` и др.)
   - имя аккаунта продавца
   - время листинга

3. Моды нормализуются и сохраняются как `mods_norm` — отсортированный список строк вида `"+# to strength"`.

Уже виденные item ID пропускаются. Если включён кэш (`--use-cache`), между запусками сохраняется `cache.json` со всеми собранными предметами.

---

### Шаг 3 — Аналитика пула кандидатов

По собранной выборке считаются:

**Топ модификаторы** — какие моды встречаются чаще всего среди дорогих предметов, с какой максимальной ценой и какой долей от всей выборки.

**Топ k-комбинации** — тройки (k=3) и четвёрки (k=4) модов для rare, пары (k=2) для magic — которые чаще всего встречаются вместе. Ранжируются по количеству совпадений.

---

### Шаг 4 — Построение финального запроса

Из топ-модов пула строится **count-запрос**: предмет должен иметь хотя бы N из M указанных модов.

```
stat_groups = [{
    "type": "count",
    "filters": [{"id": "explicit.stat_...", "value": {"min": 0}}, ...],
    "value": {"min": N}
}]
```

`N` = `count_min_match` (по умолчанию 3 для rare, 2 для magic).
`M` = количество топ-модов, но не более `max_pool_stats` (по умолчанию 60).

Для jewel дополнительно строятся weapon-specific запросы: для каждого типа оружия (bow, crossbow, spear, quarterstaff, mace) отдельно фильтруются моды, которые специфичны для этого оружия, и делается отдельный поиск с кликабельной ссылкой.

---

### Шаг 5 — Финальная выборка

`POST /api/trade2/search/` с построенным count-запросом и порогом `--final-min-div`.
Возвращает предметы с нужными комбинациями модов выше заданной цены.
Если финальный запрос не нашёл предметов (count-query слишком строгий), пайплайн завершится с предупреждением — пул-аналитика всё равно сохраняется в Excel.

---

### Шаг 6 — Экспорт в Excel

Результат записывается в `reports/poe2_{item}_{rarity}_{indexed}_{timestamp}.xlsx`.

| Лист | Содержимое |
|------|-----------|
| `pool_top_mods` | Топ модификаторы пула: count, max цена, доля |
| `pool_top_combos` | Топ k-комбинации модов пула |
| `final_query_by_weapon` | Weapon-specific запросы с кликабельными URL (только для jewel) |
| `final_raw` | Финальная выборка с ценами и модами |
| `final_top_mods` | Топ моды финальной выборки |
| `final_top_combos` | Топ комбинации финальной выборки |
| `meta` | Метаданные запуска: URL запроса, счётчики |

В листе `meta` колонка `trade_search_url` — кликабельная ссылка на Trade сайт с уже выполненным поиском.

---

## Кэш

При `--use-cache` (включён по умолчанию) все собранные предметы сохраняются в `cache.json`:

```json
{
  "ids": ["abc123", "def456", ...],
  "items": [{"item_id": "abc123", "price_div": 250.0, "mods_norm": [...], ...}]
}
```

При следующем запуске уже виденные ID пропускаются при поиске, а кэшированные предметы добавляются к новым кандидатам. Это позволяет накапливать выборку между запусками.

`--refresh-cache` — сбросить кэш перед запуском и начать заново.
`--no-cache` — полностью отключить кэш.

---

## Файл stats.json

Дамп официального API:

```
GET https://www.pathofexile.com/api/trade2/data/stats
```

Содержит все stat ID игры с текстами модов. Нужен для перевода текста мода в API-идентификатор при построении поисковых запросов.

**Обновить:**
```powershell
Invoke-WebRequest "https://www.pathofexile.com/api/trade2/data/stats" `
  -OutFile "stats.json" `
  -UserAgent "Mozilla/5.0 PoE2JewelComboStats/2.0"
```

Обновлять стоит при выходе патчей, добавляющих новые моды, и в начале каждой новой лиги.

---

## Rate limiting

Trade API GGG ограничивает количество запросов. Пайплайн:
- Делает `sleep_search` (1.5 с по умолчанию) перед каждым search-запросом
- Делает `sleep_fetch` (1.0 с) перед каждым fetch-запросом
- При ответе `429` ждёт `Retry-After` из заголовка (или exponential backoff) и повторяет до 7 раз

**Как не попасть в лимит:**

1. Используй `--stop-when-no-new` — останавливает повторные passes если нет новых предметов (API возвращает те же ID, повторы бессмысленны и тратят лимит).

2. Передай `POESESSID` — авторизованные запросы имеют более высокий лимит на стороне GGG:

```powershell
$env:POESESSID = "ВАШ_ТОКЕН"
poe2-price-modifier --stop-when-no-new
```

Токен берётся из cookies браузера (DevTools → Application → Cookies → pathofexile.com → POESESSID) когда ты залогинен.

3. Увеличь `--sleep-search 3.0` если лимит всё равно срабатывает.

---

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

---

## Запуск

**Jewel (Emerald, Standard):**
```powershell
poe2-price-modifier `
  --item-category jewel `
  --item-type Emerald `
  --rarity rare `
  --price-window 200:300 `
  --price-window 300:500 `
  --final-min-div 10 `
  --candidate-passes 5 `
  --stop-when-no-new `
  --verbose
```

**Аксессуары (амулеты / кольца / пояса):**
```powershell
poe2-price-modifier `
  --item-category accessory `
  --rarity rare `
  --price-window 50:300 `
  --price-currency exalted `
  --stop-when-no-new `
  --verbose
```

**Старт лиги (цены в exalted):**
```powershell
poe2-price-modifier `
  --item-category jewel `
  --item-type Emerald `
  --rarity rare `
  --price-window 50:300 `
  --price-window 300:1000 `
  --price-currency exalted `
  --final-min-div 20 `
  --stop-when-no-new `
  --verbose
```

**Без установки (через модуль):**
```powershell
$env:PYTHONPATH = "src"
python -m poe2_price_modifier.cli --item-category jewel --item-type Emerald --verbose
```

**Debug — посмотреть сырой ответ API:**
```powershell
poe2-price-modifier-debug --min-div 200 --max-div 300 --limit 3
```

---

## Все параметры CLI

| Параметр | По умолчанию | Описание |
|----------|-------------|----------|
| `--league` | `Standard` | Название лиги |
| `--item-category` | `jewel` | Категория предмета: `jewel`, `accessory`, `armour`, `weapon`, `flask` |
| `--item-type` | — | Базовый тип предмета (`Emerald`, `Gold Amulet`, ...). Если не указан — ищет по всей категории |
| `--item-name` | — | Точное имя (для уников) |
| `--rarity` | `rare` | `magic`, `rare`, `unique` |
| `--indexed` | `12hours` | Давность листинга (`1hour`, `12hours`, `1day`, ...) |
| `--price-window` | `200:300` | Диапазон цен для сбора (можно повторять) |
| `--final-min-div` | `10` | Мин. цена для финального запроса |
| `--max-div` | `1000` | Макс. цена (оба запроса) |
| `--price-currency` | `exalted` | `divine` или `exalted` |
| `--candidate-passes` | `5` | Количество search-pass на окно |
| `--stop-when-no-new` | off | Остановить passes, если нет новых ID (рекомендуется) |
| `--fetch-chunk` | `10` | Размер batch для fetch |
| `--max-fetch-per-search` | `100` | Макс. ID забирать из одного поиска |
| `--sleep-search` | `1.5` | Пауза перед search-запросом (сек) |
| `--sleep-fetch` | `1.0` | Пауза перед fetch-запросом (сек) |
| `--use-cache` / `--no-cache` | `--use-cache` | Включить/выключить кэш |
| `--refresh-cache` | off | Сбросить кэш перед запуском |
| `--cache-path` | `cache.json` | Путь к файлу кэша |
| `--stats-path` | `stats.json` | Путь к дампу stats API |
| `--output-dir` | `reports/` | Куда сохранять Excel-файлы |
| `--poesessid` | `$POESESSID` | Токен сессии для авторизации |
| `--verbose` | off | Подробное логирование |

### Валидные значения --item-category

| Значение | Что включает |
|----------|-------------|
| `jewel` | все джевелы (дефолт) |
| `accessory` | амулеты, кольца, пояса |
| `armour` | броня, шлемы, перчатки, сапоги, щиты |
| `weapon` | всё оружие |
| `flask` | фласки |
| `gem` | скиллы и поддержки |

---

## Архитектура

```
src/poe2_price_modifier/
├── config.py       — PipelineConfig: все параметры запуска
├── pipeline.py     — TradeClient, сбор данных, аналитика, экспорт
├── utils.py        — нормализация текста, парсинг цен, извлечение модов
├── cli.py          — точка входа poe2-price-modifier
└── debug_cli.py    — точка входа poe2-price-modifier-debug
```

**`config.py`** — единственный источник истины для дефолтов. Все параметры из CLI пишутся в `PipelineConfig` и передаются в `run_pipeline`.

**`utils.py`** — чистые функции без зависимостей от API:
- `normalize_mod_text` — числа → `#`, lowercase
- `extract_mod_lines` — вытащить все строки модов из item JSON
- `parse_price` — проверить валюту и вернуть float
- `expand_bracket_variants` — раскрыть `[bow|crossbow]` в оба варианта

**`pipeline.py`** — основная логика:
- `TradeClient` — HTTP-клиент с retry/backoff
- `load_stat_map` — парсинг stats.json в словарь текст→stat_id
- `collect_candidates` — многопроходный сбор предметов
- `build_count_stat_group` — построение count-запроса из списка модов
- `build_weapon_queries` — weapon-specific запросы (только для jewel)
- `run_pipeline` — оркестратор всего пайплайна

---

## Тесты

```powershell
pip install -e .[dev]
pytest
```
