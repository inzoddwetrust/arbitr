# Phase 0: Proof of Concept — Отчёт

## Цель

Доказать возможность автоматизированного парсинга сайта kad.arbitr.ru (Картотека арбитражных дел) с обходом WASM-антибота.

## Результат: ✅ УСПЕХ

Все ключевые функции реализованы и протестированы:
- Поиск дела по номеру
- Извлечение данных из результатов поиска
- Навигация на карточку дела
- Парсинг всех инстанций дела
- **Скачивание PDF-документов**

---

## Ключевые технические находки

### 1. WASM Antibot Protection

Сайт kad.arbitr.ru использует серьёзную защиту на базе WebAssembly:

```
/Content/Static/js/common/fp_bg.wasm
```

**Механизм работы:**
1. При первом запросе сервер отдаёт HTML с формой и закодированными данными
2. JavaScript декодирует данные (бинарное кодирование через табы/пробелы)
3. WASM модуль вычисляет fingerprint браузера
4. Форма автоматически отправляется с токеном
5. Сервер устанавливает cookie и редиректит на контент

**HTML структура антибота:**
```html
<div id="salto" style="display:none">xcd67qm4bns</div>
<form id="searchForm" method="post">
    <input id="token" name="token" value="...">
    <input id="hash" name="hash" value="...">
</form>
<input id="datat" type="hidden" value="[табы и пробелы]">
<script>eval(decode("ret"))</script>
```

### 2. Firefox — ОБЯЗАТЕЛЕН

| Браузер | Результат |
|---------|-----------|
| Chromium | ❌ Бесконечный редирект, WASM не проходит |
| Firefox | ✅ WASM отрабатывает корректно |

**Причина:** Firefox иначе обрабатывает некоторые Web API, которые использует WASM для fingerprinting.

```python
browser = await p.firefox.launch(headless=True)
```

### 3. Response Interceptor для PDF

Прямые запросы к PDF возвращают HTML с антиботом. Решение:

```python
pdf_content = None

async def handle_response(response):
    nonlocal pdf_content
    if "Pdf" in response.url and "application/pdf" in response.headers.get("content-type", ""):
        pdf_content = await response.body()

pdf_page.on("response", handle_response)
await pdf_page.goto(pdf_url, wait_until="domcontentloaded")
```

**Ключевой инсайт:** Сервер отдаёт PDF в response ДО того, как WASM блокирует отображение. Interceptor ловит контент на лету.

### 4. URL Patterns

| Тип | URL Pattern |
|-----|-------------|
| Поиск | `https://kad.arbitr.ru/` (POST form) |
| Карточка дела | `https://kad.arbitr.ru/Card/{GUID}` |
| PDF (запрос) | `https://kad.arbitr.ru/Kad/PdfDocument/{caseGuid}/{docGuid}/{filename}.pdf` |
| PDF (редирект) | `https://kad.arbitr.ru/Document/Pdf/{caseGuid}/{docGuid}/{filename}.pdf?isAddStamp=True` |

### 5. Критичные селекторы

```python
# Страница поиска
SEARCH_INPUT = "#sCase"
SEARCH_BUTTON = "#b-form-submit button[alt='Найти']"

# Результаты поиска
RESULT_ROW = "//div[@class='b-cases']/table/tbody/tr"
CASE_LINK = ".//td[1]//a"
PLAINTIFF = ".//td[2]//span[@class='js-rolloverHtml']"
DEFENDANT = ".//td[3]//span[@class='js-rolloverHtml']"
COURT = ".//td[4]"
JUDGE = ".//td[5]//span[@class='js-rolloverHtml']"
DATE = ".//td[6]"

# Карточка дела
CASE_STATUS = "#gr_case_statustext, .b-case-header .state"
INSTANCE_BLOCK = "div#blocks > div > div:has(ul#defined__tabs)"
INSTANCE_TAB = "ul#defined__tabs li a"
PDF_LINK = "h2.b-case-result a[href*='PdfDocument']"
```

### 6. Структура данных

**Результат поиска:**
```python
{
    "case_number": "А40-57726/2024",
    "case_link": "https://kad.arbitr.ru/Card/cfd61845-...",
    "plaintiff": "ООО \"КОМПАНИЯ\"",
    "defendant": "ИП Иванов И.И.",
    "court": "АС города Москвы",
    "judge": "Петров П.П.",
    "date": "21.03.2024"
}
```

**Карточка дела:**
```python
{
    "case_number": "А40-57726/2024",
    "guid": "cfd61845-e959-42f5-a558-43d21e4090f0",
    "status": "Рассмотрение дела завершено",
    "instances": [
        {
            "court_code": "9AAS",
            "instance_id": "31b4b201-...",
            "instance_type": "Апелляционная инстанция",
            "reg_date": "02.10.2024",
            "case_number": "09АП-43795/2024",
            "court_name": "9 арбитражный апелляционный суд",
            "decision_pdf": "https://kad.arbitr.ru/Kad/PdfDocument/...",
            "decision_text": "Оставить без изменения..."
        }
    ]
}
```

---

## Тестовые данные

| Параметр | Значение |
|----------|----------|
| Номер дела | А40-57726/2024 |
| GUID | cfd61845-e959-42f5-a558-43d21e4090f0 |
| Инстанций | 2 |
| PDF файлов | 2 |

---

## Зависимости

```
playwright>=1.40.0
```

**Установка браузера:**
```bash
playwright install firefox
```

---

## Известные ограничения

1. **Только Firefox** — Chromium не работает с WASM антиботом
2. **Скорость** — каждый PDF требует открытия новой вкладки (~3-5 сек)
3. **Rate limiting** — не исследован, возможны блокировки при массовом парсинге
4. **Вкладка "Электронное дело"** — содержит больше документов, не парсится в PoC

---

## Файлы

| Файл | Описание |
|------|----------|
| `poc_crawler.py` | Рабочий PoC краулер |
| `downloads/` | Папка для скачанных PDF |

---

## Время выполнения (тестовый кейс)

| Этап | Время |
|------|-------|
| Запуск браузера | ~2 сек |
| Поиск + результаты | ~3 сек |
| Переход на карточку | ~2 сек |
| Парсинг карточки | ~1 сек |
| Скачивание 2 PDF | ~15 сек |
| **Итого** | ~23 сек |

---

## Выводы

1. **kad.arbitr.ru парсится** — несмотря на WASM защиту
2. **Firefox + Playwright** — рабочая комбинация
3. **Response interceptor** — ключ к скачиванию PDF
4. **Готово к масштабированию** — архитектура позволяет добавить очереди, параллелизм, хранилище
