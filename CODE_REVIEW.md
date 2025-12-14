# Code Review: KAD Parser

## Обзор

Проект представляет собой парсер для kad.arbitr.ru (Картотека арбитражных дел) с обходом WASM-защиты. Текущее состояние: **Phase 0 (PoC) завершён**.

**Проанализированные файлы:**
- `poc2.py` (1248 строк) - основная реализация
- `poc.py` (761 строка) - ранняя версия
- `pdf2json.py` (85 строк) - утилита извлечения текста

---

## Критические проблемы

### 1. Безопасность и обработка ошибок

#### 1.1 Пустые `except` блоки
**Файл:** `poc2.py:186-188`, `poc2.py:293`, `poc2.py:433`

```python
# Плохо - глушится любая ошибка
try:
    page_text = await page.inner_text("body")
    ...
except:
    pass
```

**Рекомендация:** Указывать конкретные исключения и логировать:
```python
except PlaywrightError as e:
    log.debug(f"Rate limit check failed: {e}")
```

#### 1.2 MD5 для идентификаторов
**Файл:** `poc2.py:229-230`

```python
url_hash = hashlib.md5(url.encode()).hexdigest()
```

MD5 криптографически устарел. Для идентификаторов лучше использовать SHA-256 или UUID.

**Рекомендация:**
```python
import hashlib
url_hash = hashlib.sha256(url.encode()).hexdigest()[:32]
```

---

### 2. Архитектурные проблемы

#### 2.1 Отсутствие разделения ответственности (SRP)
**Файл:** `poc2.py` - весь файл

1248 строк в одном файле с смешанными обязанностями:
- Модели данных
- HTTP/Browser автоматизация
- Парсинг HTML
- Работа с файлами
- Бизнес-логика

**Рекомендация:** Разбить на модули:
```
src/
├── models/        # Pydantic/dataclass модели
├── browser/       # Playwright автоматизация
├── parsers/       # HTML парсеры для каждой вкладки
├── storage/       # Сохранение файлов и прогресса
└── cli.py         # Точка входа
```

#### 2.2 Хардкод конфигурации
**Файл:** `poc2.py:68-79`

```python
BASE_URL = "https://kad.arbitr.ru/"
HEADLESS = True
SLOW_MO = 100
DELAY_BETWEEN_DOCS_BASE = 3.0
```

**Рекомендация:** Использовать Pydantic Settings или конфиг-файл:
```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    base_url: str = "https://kad.arbitr.ru/"
    headless: bool = True
    slow_mo: int = 100

    class Config:
        env_prefix = "KAD_"
```

#### 2.3 Дублирование кода между poc.py и poc2.py
Функции `search_by_case_number`, `navigate_to_case_card`, `click_ed_tab` и др. практически идентичны в обоих файлах.

**Рекомендация:** Удалить `poc.py` или извлечь общий код в библиотеку.

---

### 3. Проблемы производительности

#### 3.1 Синхронная запись файлов в async контексте
**Файл:** `poc2.py:875-878`

```python
doc_file.write_text(
    json.dumps(asdict(doc), ensure_ascii=False, indent=2),
    encoding="utf-8"
)
```

**Рекомендация:** Использовать `aiofiles`:
```python
import aiofiles

async with aiofiles.open(doc_file, 'w', encoding='utf-8') as f:
    await f.write(json.dumps(asdict(doc), ensure_ascii=False, indent=2))
```

#### 3.2 Множественное преобразование `asdict()` для одного объекта
**Файл:** `poc2.py:1148-1154`

```python
full_doc = DocumentFull(
    **asdict(doc),  # Конвертация в dict
    ...
)
```

**Рекомендация:** Добавить метод `to_full()` в `DocumentMeta`:
```python
def to_full(self, text: str, requires_ocr: bool) -> DocumentFull:
    return DocumentFull(
        **{k: getattr(self, k) for k in self.__dataclass_fields__},
        has_text=bool(text),
        requires_ocr=requires_ocr,
        char_count=len(text),
        text=text,
    )
```

#### 3.3 Неоптимальный поиск максимума страниц
**Файл:** `poc2.py:641-648`, `poc2.py:723-729`

```python
for i in range(pagination_count):
    page_num_str = await pagination_items.nth(i).get_attribute("data-page_num")
    ...
    max_page = max(max_page, int(page_num_str))
```

**Рекомендация:** Использовать `evaluate` для одного запроса:
```python
max_page = await page.evaluate("""
    () => Math.max(...Array.from(
        document.querySelectorAll('.js-chrono-pagination-pager-item[data-page_num]')
    ).map(el => parseInt(el.dataset.page_num) || 1))
""")
```

---

### 4. Качество кода

#### 4.1 Отсутствие типизации для возвращаемых значений
**Файл:** `poc2.py:278`, `poc2.py:561`

```python
async def parse_document_metadata(link_element: Locator, source_tab: str, instance_id: Optional[str] = None) -> Optional[DocumentMeta]:
```

Хорошо, но многие функции не типизированы.

**Рекомендация:** Добавить `-> None`, `-> bool`, `-> list[X]` везде.

#### 4.2 Магические числа
**Файл:** `poc2.py:859`

```python
requires_ocr = len(text) < 100  # Likely a scan if very little text
```

**Рекомендация:** Вынести в константу:
```python
MIN_TEXT_LENGTH_FOR_OCR = 100
requires_ocr = len(text) < MIN_TEXT_LENGTH_FOR_OCR
```

#### 4.3 Использование f-string для логирования
**Файл:** везде

```python
log.info(f"[{idx}/{total}] ✅ {filename[:50]}... ({file_size:.1f} KB)")
```

Это создаёт строку даже если уровень логирования выше INFO.

**Рекомендация:** Для debug-логов использовать ленивое форматирование:
```python
log.debug("Processing %s", filename)  # Строка создаётся только если нужна
```

---

### 5. Надёжность

#### 5.1 Отсутствие валидации входных данных
**Файл:** `poc2.py:1196-1202`

```python
case_number = sys.argv[1]  # Нет валидации формата
```

**Рекомендация:** Добавить валидацию:
```python
import re

CASE_NUMBER_PATTERN = re.compile(r'^[АA]\d+-\d+/\d{4}$')

def validate_case_number(case_number: str) -> str:
    if not CASE_NUMBER_PATTERN.match(case_number):
        raise ValueError(f"Invalid case number format: {case_number}")
    return case_number
```

#### 5.2 Нет graceful shutdown при SIGINT/SIGTERM
**Файл:** `poc2.py:1193-1248`

При Ctrl+C браузер может остаться открытым.

**Рекомендация:**
```python
import signal

async def shutdown(browser, signal_received):
    log.info(f"Received {signal_received.name}, shutting down...")
    await browser.close()
    sys.exit(0)

# В main():
loop = asyncio.get_event_loop()
for sig in (signal.SIGINT, signal.SIGTERM):
    loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(browser, s)))
```

#### 5.3 Отсутствие retry для сетевых операций
**Файл:** `poc2.py:459`

```python
await page.goto(case_url, wait_until="domcontentloaded", timeout=30000)
```

Одна неудача - вся операция провалена.

**Рекомендация:** Использовать `tenacity`:
```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def safe_goto(page, url, **kwargs):
    await page.goto(url, **kwargs)
```

---

### 6. Тестируемость

#### 6.1 Нет unit-тестов

**Рекомендация:** Добавить тесты с pytest и pytest-asyncio:

```python
# tests/test_parsers.py
import pytest
from src.parsers import extract_guid_from_url, extract_date_from_filename

def test_extract_guid_from_url():
    url = "https://kad.arbitr.ru/PdfDocument/abc-123/def-456/file.pdf"
    case_guid, doc_guid = extract_guid_from_url(url)
    assert case_guid == "abc-123"
    assert doc_guid == "def-456"

def test_extract_date_from_filename():
    assert extract_date_from_filename("A60-21280-2023_20251204_Opredelenie.pdf") == "2025-12-04"
    assert extract_date_from_filename("invalid.pdf") is None
```

#### 6.2 Сложно мокировать зависимости

Функции напрямую используют глобальные объекты и Playwright.

**Рекомендация:** Внедрение зависимостей:
```python
class CaseParser:
    def __init__(self, page: Page, config: Settings):
        self.page = page
        self.config = config

    async def search(self, case_number: str) -> bool:
        ...
```

---

### 7. Документация

#### 7.1 Отсутствие docstrings для классов
**Файл:** `poc2.py:91-147`

```python
@dataclass
class DocumentMeta:
    """Metadata for a single document."""  # Только одна строка
```

**Рекомендация:** Добавить полные docstrings с описанием полей:
```python
@dataclass
class DocumentMeta:
    """
    Metadata for a single document from kad.arbitr.ru.

    Attributes:
        doc_id: Unique GUID extracted from PDF URL.
        case_guid: GUID of the parent case.
        url: Full URL to download the PDF document.
        filename: Original filename from the server.
        date: Document date in ISO format (YYYY-MM-DD), extracted from filename.
        doc_type: Type of document (Определение, Решение, etc.).
        ...
    """
```

---

## Приоритеты улучшений

| Приоритет | Категория | Описание |
|-----------|-----------|----------|
| 🔴 **P0** | Надёжность | Обработка ошибок, retry, graceful shutdown |
| 🟠 **P1** | Архитектура | Разделение на модули, конфиг через env |
| 🟡 **P2** | Качество | Типизация, валидация, константы |
| 🟢 **P3** | Производительность | aiofiles, оптимизация JS-запросов |
| 🔵 **P4** | Тестирование | Unit-тесты, интеграционные тесты |

---

## Рекомендуемая структура проекта (Phase 1)

```
kad-parser/
├── src/
│   ├── __init__.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── document.py      # DocumentMeta, DocumentFull
│   │   ├── case.py          # CaseInfo, Instance
│   │   └── progress.py      # Progress
│   ├── browser/
│   │   ├── __init__.py
│   │   ├── context.py       # Browser setup, stealth
│   │   └── navigation.py    # Search, navigate
│   ├── parsers/
│   │   ├── __init__.py
│   │   ├── court_acts.py
│   │   ├── cards.py
│   │   └── electronic_case.py
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── filesystem.py    # Save JSON, PDF
│   │   └── progress.py      # Progress tracking
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── delays.py        # Human-like delays
│   │   └── extractors.py    # GUID, date extraction
│   ├── config.py            # Pydantic Settings
│   └── cli.py               # Typer CLI
├── tests/
│   ├── __init__.py
│   ├── test_parsers.py
│   ├── test_extractors.py
│   └── conftest.py          # Fixtures
├── pyproject.toml
├── README.md
└── .env.example
```

---

## Быстрые исправления (можно сделать сейчас)

### 1. Исправить пустые except блоки

```python
# poc2.py:186-188
except Exception:  # Вместо bare except
    pass

# poc2.py:293
except (TimeoutError, AttributeError):
    pass
```

### 2. Добавить константы для магических чисел

```python
# В начале файла после RATE_LIMIT_PHRASES
MIN_TEXT_LENGTH_FOR_OCR = 100
PROGRESS_SAVE_INTERVAL = 10
DEFAULT_PDF_TIMEOUT_MS = 60000
```

### 3. Добавить requirements.txt

```
playwright>=1.40.0
playwright-stealth>=1.0.6
pymupdf>=1.23.0
```

### 4. Добавить .gitignore

```
__pycache__/
*.pyc
.env
downloads/
output/
case_*/
*.log
```

---

## Заключение

Код представляет собой **качественный PoC** с хорошо решённой основной задачей - обходом WASM-защиты через Firefox и response interceptor.

Основные сильные стороны:
- Работающий обход антибот-защиты
- Человекоподобные задержки
- Graceful stop с сохранением прогресса
- Дедупликация документов из разных источников

Для перехода в production (Phase 1+) необходимо:
1. Рефакторинг в модульную структуру
2. Добавление unit-тестов
3. Улучшение обработки ошибок
4. Внедрение конфигурации через environment

Оценка зрелости: **6/10** (хороший PoC, требует доработки для production).
