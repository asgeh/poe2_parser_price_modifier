# POE2 Price Modifier Analyzer

Профессионально структурированный Python-проект для анализа дорогих jewel-ов в трейде Path of Exile и построения отчётов по комбинациям модификаторов.

## Что изменено относительно исходного скрипта

- Добавлена пакетная структура (`src/poe2_price_modifier`).
- Вынесена конфигурация в dataclass `PipelineConfig`.
- Добавлен CLI с параметрами командной строки.
- Логика API и аналитики разделена на отдельные слои.
- Добавлены автотесты для ключевых чистых функций.
- Настроен запуск как консольная команда через `pyproject.toml`.

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Windows (PowerShell):
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

## Быстрый запуск

```bash
poe2-price-modifier \
  --league "Fate of the Vaal" \
  --item-type "Emerald" \
  --rarity rare \
  --final-min-div 10 \
  --price-window 200:300 \
  --stats-path stats.json
```

Результат: Excel-файл с листами кандидатов, пула комбинаций, финальных результатов и метаданных.
Файлы сохраняются в папку `reports/`.

## Памятка запуска

1. Обычный запуск (установленный CLI):
```bash
poe2-price-modifier
```

2. Запуск с параметрами:
```bash
poe2-price-modifier --league "Fate of the Vaal" --item-type "Emerald" --rarity rare --candidate-passes 5 --final-min-div 10
```

3. Запуск без установки (через модуль):
```bash
PYTHONPATH=src python -m poe2_price_modifier.cli --candidate-passes 5 --verbose
```

Windows (PowerShell):
```powershell
$env:PYTHONPATH="src"
python -m poe2_price_modifier.cli --candidate-passes 5 --verbose
```

## Запуск тестов

```bash
pip install -e .[dev]
pytest
```

## Архитектура

- `config.py` — структура конфигурации пайплайна.
- `pipeline.py` — клиент API, нормализация модов, сбор/ранжирование и экспорт отчётов.
- `cli.py` — аргументы командной строки и запуск пайплайна.

