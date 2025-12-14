# KAD Parser: Архитектура и План Развития

> Комплексный документ по развитию парсера kad.arbitr.ru в LLM-аналитическую платформу для юристов.

---

## Содержание

1. [Обзор проекта](#1-обзор-проекта)
2. [Текущее состояние (Phase 0)](#2-текущее-состояние-phase-0)
3. [Выявленные проблемы](#3-выявленные-проблемы)
4. [LLM-аналитика для юристов](#4-llm-аналитика-для-юристов)
5. [RAG-система](#5-rag-система)
6. [Целевая архитектура](#6-целевая-архитектура)
7. [План реализации](#7-план-реализации)

---

## 1. Обзор проекта

### 1.1 Цель

Создание платформы для автоматического анализа арбитражных дел с помощью LLM:
- **Парсинг** дел с kad.arbitr.ru (обход WASM-защиты)
- **Извлечение** структурированных данных из судебных документов
- **Анализ** сильных/слабых мест сторон
- **Рекомендации** юристам по стратегии

### 1.2 Ключевые пользователи

| Роль | Задача | Как помогает система |
|------|--------|---------------------|
| Юрист истца | Подготовка позиции | Анализ слабых мест ответчика, подбор практики |
| Юрист ответчика | Защита | Поиск процессуальных нарушений, контраргументы |
| Аналитик | Due diligence | Быстрый обзор судебной истории компании |
| Судья/помощник | Подготовка к заседанию | Структурированная хронология дела |

---

## 2. Текущее состояние (Phase 0)

### 2.1 Что реализовано

```
arbitr/
├── poc2.py          # Основной парсер (1248 строк)
├── poc.py           # Ранняя версия (761 строка)
├── pdf2json.py      # Утилита извлечения текста (85 строк)
├── ROADMAP.md       # План развития
├── PHASE0_REPORT.md # Отчёт о Phase 0
└── CODE_REVIEW.md   # Ревью кода
```

### 2.2 Возможности парсера

| Функция | Статус | Описание |
|---------|--------|----------|
| Обход WASM-защиты | ✅ | Firefox + response interceptor |
| Поиск по номеру дела | ✅ | Через suggest dropdown |
| Парсинг "Судебные акты" | ✅ | Без пагинации |
| Парсинг "Карточки" | ✅ | С аккордеонами и пагинацией |
| Парсинг "Электронное дело" | ✅ | С пагинацией |
| Скачивание PDF | ✅ | Через response interceptor |
| Извлечение текста | ✅ | PyMuPDF |
| Human-like задержки | ✅ | Рандомизированные паузы |
| Graceful stop | ✅ | Сохранение прогресса при rate limit |
| Дедупликация | ✅ | По doc_id |

### 2.3 Структура вывода

```
case_A60-21280-2023/
├── README.md              # Человекочитаемое описание
├── case.json              # Метаданные дела
├── court_acts.json        # Список судебных актов
├── electronic_case.json   # Список документов из ЭД
├── instances/
│   └── inst_{guid}.json   # Документы по инстанциям
├── documents/
│   └── {guid}.json        # Полные тексты документов
└── _progress.json         # Прогресс скачивания
```

---

## 3. Выявленные проблемы

### 3.1 Критические (P0)

#### 3.1.1 Проблема с refresh сессии после перерыва

**Симптом:** После `take_break()` первые 3-5 документов не скачиваются.

**Причина:** Недостаточное время ожидания после refresh для инициализации WASM.

```python
# Текущий код (poc2.py:1118-1123)
await page.goto(case_info.url, wait_until="domcontentloaded", timeout=30000)
await page.wait_for_timeout(2000)  # ← Недостаточно!
```

**Решение:**
```python
async def refresh_session_with_warmup(page: Page, case_url: str) -> None:
    """Refresh сессии с прогревом WASM."""

    # 1. Перейти на страницу дела
    await page.goto(case_url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

    # 2. Прогреть WASM через открытие любого PDF
    pdf_link = page.locator("a[href*='PdfDocument']").first
    if await pdf_link.count() > 0:
        url = await pdf_link.get_attribute("href")
        if url:
            warmup_page = await page.context.new_page()
            try:
                await warmup_page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await warmup_page.wait_for_timeout(5000)  # Дождаться WASM
            except Exception as e:
                log.debug(f"Warmup failed (expected): {e}")
            finally:
                await warmup_page.close()

    # 3. Вернуться к вкладке судебных актов для стабильности
    acts_tab = page.locator("#case_acts")
    if await acts_tab.count() > 0:
        await acts_tab.click()
        await page.wait_for_timeout(2000)
```

#### 3.1.2 Пустые except блоки

**Проблема:** Глушатся все ошибки, усложняя отладку.

```python
# Плохо (poc2.py:186-188)
except:
    pass

# Хорошо
except PlaywrightError as e:
    log.debug(f"Rate limit check failed: {e}")
```

#### 3.1.3 Отсутствие graceful shutdown

**Проблема:** При Ctrl+C браузер может остаться открытым.

**Решение:**
```python
import signal
import sys

class GracefulShutdown:
    def __init__(self):
        self.shutdown_requested = False
        signal.signal(signal.SIGINT, self._handler)
        signal.signal(signal.SIGTERM, self._handler)

    def _handler(self, signum, frame):
        log.info(f"Получен сигнал {signum}, завершаем...")
        self.shutdown_requested = True

# Использование
shutdown = GracefulShutdown()

for doc in docs_to_download:
    if shutdown.shutdown_requested:
        save_progress(output_dir, progress)
        break
    # ... download logic
```

### 3.2 Архитектурные (P1)

#### 3.2.1 Монолитный файл

**Проблема:** 1248 строк в одном файле, смешение ответственностей.

**Решение:** Модульная структура (см. раздел 6).

#### 3.2.2 Хардкод конфигурации

**Проблема:**
```python
BASE_URL = "https://kad.arbitr.ru/"
HEADLESS = True
DELAY_BETWEEN_DOCS_BASE = 3.0
```

**Решение:** Pydantic Settings + .env:
```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    base_url: str = "https://kad.arbitr.ru/"
    headless: bool = True
    delay_docs_base: float = 3.0
    delay_docs_jitter: float = 2.0
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    class Config:
        env_prefix = "KAD_"
        env_file = ".env"
```

### 3.3 Качество кода (P2)

| Проблема | Где | Решение |
|----------|-----|---------|
| MD5 для ID | poc2.py:229 | Использовать SHA-256 |
| Магические числа | poc2.py:859 | Вынести в константы |
| f-string в логах | везде | Ленивое форматирование для debug |
| Нет валидации ввода | poc2.py:1196 | Regex для номера дела |

### 3.4 Производительность (P3)

| Проблема | Решение |
|----------|---------|
| Синхронная запись файлов | aiofiles |
| Множественные вызовы в цикле | JavaScript batch queries |
| Повторные asdict() | Метод to_full() в dataclass |

### 3.5 Тестирование (P4)

**Проблема:** Нет unit-тестов.

**Решение:**
```python
# tests/test_extractors.py
import pytest
from src.utils.extractors import extract_guid_from_url, extract_date_from_filename

def test_extract_guid_from_url():
    url = "https://kad.arbitr.ru/PdfDocument/abc-123/def-456/file.pdf"
    case_guid, doc_guid = extract_guid_from_url(url)
    assert case_guid == "abc-123"
    assert doc_guid == "def-456"

def test_extract_guid_fallback():
    url = "https://example.com/unknown/path"
    case_guid, doc_guid = extract_guid_from_url(url)
    assert case_guid == ""
    assert len(doc_guid) == 32  # MD5 hash

def test_extract_date_from_filename():
    assert extract_date_from_filename("A60-21280-2023_20251204_Opredelenie.pdf") == "2025-12-04"
    assert extract_date_from_filename("invalid.pdf") is None
    assert extract_date_from_filename("A60_20231301_Doc.pdf") is None  # Invalid date
```

---

## 4. LLM-аналитика для юристов

### 4.1 Целевые сценарии использования

```
┌─────────────────────────────────────────────────────────────────┐
│                    СЦЕНАРИИ ИСПОЛЬЗОВАНИЯ                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. АНАЛИЗ СВОЕГО ДЕЛА                                          │
│     Юрист загружает дело → Получает:                            │
│     • Структурированную хронологию                              │
│     • Анализ сильных/слабых мест обеих сторон                   │
│     • Рекомендации по стратегии                                 │
│     • Релевантную судебную практику                             │
│                                                                 │
│  2. АНАЛИЗ ДЕЛА ПРОТИВНИКА                                      │
│     • Поиск процессуальных нарушений                            │
│     • Выявление противоречий в позиции                          │
│     • Подготовка контраргументов                                │
│                                                                 │
│  3. DUE DILIGENCE                                               │
│     • Анализ судебной истории компании                          │
│     • Выявление паттернов (частые споры, типичные претензии)    │
│     • Оценка репутационных рисков                               │
│                                                                 │
│  4. ПОДГОТОВКА К ЗАСЕДАНИЮ                                      │
│     • Quick brief по делу                                       │
│     • Ключевые вопросы для обсуждения                           │
│     • Возможные возражения сторон                               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 Проблема: объём данных

```
Типичное банкротное дело:
• 100-500 документов
• ~50-200 токенов на страницу PDF
• Итого: 500K - 2M+ токенов

Контекст LLM:
• Claude: 200K токенов
• GPT-4: 128K токенов

→ Невозможно загрузить всё дело целиком!
```

### 4.3 Решение: иерархическая структура

```
┌─────────────────────────────────────────────────────────────────┐
│ УРОВЕНЬ 1: Case Summary (всегда в контексте)                    │
│ • Стороны, суть спора, хронология, текущий статус               │
│ • ~2-5K токенов                                                 │
├─────────────────────────────────────────────────────────────────┤
│ УРОВЕНЬ 2: Document Summaries (индексированы для поиска)        │
│ • Резюме каждого документа с ключевыми фактами                  │
│ • ~200-500 токенов на документ                                  │
├─────────────────────────────────────────────────────────────────┤
│ УРОВЕНЬ 3: Full Documents (RAG retrieval)                       │
│ • Полные тексты, подгружаются по запросу                        │
│ • Chunked по смысловым блокам                                   │
└─────────────────────────────────────────────────────────────────┘
```

### 4.4 Обогащённая модель данных

```python
@dataclass
class CaseSummary:
    """Уровень 1: Всегда в контексте LLM."""
    case_number: str
    court: str
    case_type: str  # банкротство, взыскание, оспаривание

    # Стороны
    plaintiff: Party
    defendant: Party
    third_parties: list[Party]

    # Суть
    subject: str  # "Взыскание 15.2 млн руб. по договору поставки"
    claimed_amount: Optional[Decimal]

    # Хронология ключевых событий
    timeline: list[TimelineEvent]

    # Текущий статус
    current_stage: str  # "Первая инстанция", "Апелляция", "Кассация"
    last_decision: str
    next_hearing: Optional[date]

    # Статистика
    total_documents: int
    hearings_count: int


@dataclass
class EnrichedDocument:
    """Уровень 2-3: Обогащённый документ."""
    # Базовые метаданные (уже есть)
    doc_id: str
    date: str
    doc_type: str
    text: str

    # НОВОЕ: Классификация
    document_category: str  # CLAIM, RESPONSE, RULING, EVIDENCE, etc.

    # НОВОЕ: Структурированный контент
    sections: dict[str, str]  # {"резолютивная": "...", "мотивировочная": "..."}

    # НОВОЕ: Извлечённые сущности
    entities: DocumentEntities

    # НОВОЕ: Связи
    references: list[DocumentReference]  # Ссылки на другие документы дела
    legal_refs: list[LegalReference]     # Ссылки на НПА и практику

    # НОВОЕ: LLM-сгенерированное
    summary: str            # 2-3 предложения
    key_facts: list[str]    # Ключевые факты для поиска
    key_arguments: list[str] # Ключевые аргументы


@dataclass
class DocumentEntities:
    """Извлечённые сущности из документа."""
    amounts: list[Money]      # {"value": 15200000, "currency": "RUB", "context": "сумма иска"}
    dates: list[DateEntity]   # {"date": "2023-05-15", "context": "дата договора"}
    parties: list[str]        # Упомянутые стороны
    articles: list[str]       # ["ст. 61.2 ФЗ о банкротстве", "ст. 10 ГК РФ"]
    case_refs: list[str]      # ["А40-12345/2022", "Определение ВС РФ от..."]
```

### 4.5 Классификация документов

```python
DOCUMENT_CATEGORIES = {
    # Процессуальные документы сторон
    "CLAIM": "Исковое заявление",
    "RESPONSE": "Отзыв на иск",
    "REPLY": "Возражения на отзыв",
    "OBJECTION": "Возражения",
    "MOTION": "Ходатайство",
    "APPEAL": "Апелляционная жалоба",
    "CASSATION": "Кассационная жалоба",

    # Судебные акты
    "RULING_ACCEPT": "Определение о принятии",
    "RULING_POSTPONE": "Определение об отложении",
    "RULING_INTERIM": "Определение об обеспечительных мерах",
    "RULING_OTHER": "Определение (прочее)",
    "DECISION": "Решение",
    "APPEAL_DECISION": "Постановление апелляции",
    "CASSATION_DECISION": "Постановление кассации",

    # Доказательства
    "CONTRACT": "Договор",
    "PAYMENT": "Платёжный документ",
    "ACT": "Акт",
    "INVOICE": "Счёт-фактура",
    "CORRESPONDENCE": "Переписка",
    "EXPERT": "Заключение эксперта",
    "OTHER_EVIDENCE": "Иное доказательство",

    # Прочее
    "PROTOCOL": "Протокол судебного заседания",
    "POWER_OF_ATTORNEY": "Доверенность",
    "OTHER": "Прочий документ",
}
```

### 4.6 Промпты для анализа

#### Извлечение требований из иска

```python
EXTRACT_CLAIMS_PROMPT = """Из текста искового заявления извлеки структурированную информацию.

## Текст документа
{text}

## Формат ответа (JSON)
```json
{
  "requirements": {
    "main": "Основное требование",
    "additional": ["Дополнительные требования"],
    "amounts": [{"value": 0, "currency": "RUB", "description": ""}]
  },
  "grounds": {
    "facts": ["Фактические обстоятельства"],
    "legal_basis": ["Правовые нормы"],
    "evidence_mentioned": ["Упомянутые доказательства"]
  },
  "weaknesses": {
    "procedural": ["Процессуальные риски"],
    "substantive": ["Материальные риски"],
    "evidence": ["Проблемы с доказательствами"]
  }
}
```"""
```

#### Анализ слабых мест стороны

```python
FIND_WEAKNESSES_PROMPT = """Проанализируй материалы дела и найди слабые места позиции {party}.

## Информация о деле
{case_summary}

## Релевантные документы
{documents}

## Чеклист для анализа

### Процессуальные нарушения
- [ ] Соблюдён ли претензионный порядок?
- [ ] Правильная ли подсудность?
- [ ] Соблюдены ли сроки исковой давности?
- [ ] Надлежащие ли стороны в процессе?
- [ ] Все ли доказательства надлежаще оформлены?
- [ ] Заявлены ли необходимые ходатайства?

### Материальные слабости
- [ ] Верно ли применены нормы права?
- [ ] Учтена ли актуальная практика ВС РФ?
- [ ] Есть ли противоречия в позиции?
- [ ] Достаточна ли доказательственная база?
- [ ] Доказаны ли все элементы состава?

### Тактические упущения
- [ ] Использованы ли обеспечительные меры?
- [ ] Заявлено ли о фальсификации при наличии оснований?
- [ ] Привлечены ли необходимые третьи лица?
- [ ] Назначена ли экспертиза при необходимости?

## Формат ответа

### 1. Критические проблемы (могут привести к проигрышу)
...

### 2. Существенные недостатки (ослабляют позицию)
...

### 3. Тактические замечания (можно улучшить)
...

### 4. Рекомендации для противной стороны
...
"""
```

#### Chain-of-thought юридический анализ

```python
LEGAL_ANALYSIS_PROMPT = """Ты - опытный арбитражный юрист. Проанализируй дело пошагово.

## Дело
{case_summary}

## Ключевые документы
{relevant_documents}

## Вопрос
{question}

## Инструкция: думай пошагово

### Шаг 1: ФАКТЫ
Какие факты установлены судом? Какие оспариваются сторонами?

### Шаг 2: КВАЛИФИКАЦИЯ
Какие нормы права применимы? Есть ли спор о правовой квалификации?

### Шаг 3: ПОЗИЦИИ СТОРОН
| Сторона | Сильные стороны | Слабые стороны |
|---------|-----------------|----------------|
| Истец   | ...             | ...            |
| Ответчик| ...             | ...            |

### Шаг 4: ПРОЦЕССУАЛЬНЫЕ НЮАНСЫ
- Соблюдены ли сроки?
- Правильно ли распределено бремя доказывания?
- Есть ли процессуальные нарушения?

### Шаг 5: СУДЕБНАЯ ПРАКТИКА
Какая практика ВС РФ применима? Есть ли противоречивая практика?

### Шаг 6: ПРОГНОЗ И РЕКОМЕНДАЦИИ
Вероятный исход и рекомендуемые действия для каждой стороны.
"""
```

---

## 5. RAG-система

### 5.1 Архитектура RAG

```
                    ┌─────────────────┐
                    │   Пользователь  │
                    │  "Какие доводы  │
                    │  по давности?"  │
                    └────────┬────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                     QUERY PROCESSING                            │
├─────────────────────────────────────────────────────────────────┤
│  1. Query Expansion (синонимы юр. терминов)                     │
│     "давность" → ["исковая давность", "пропуск срока", ...]     │
│                                                                 │
│  2. HyDE (опционально)                                          │
│     Генерация гипотетического ответа для лучшего поиска         │
│                                                                 │
│  3. Query Classification                                        │
│     Определение типа: фактический / правовой / процессуальный   │
└─────────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                        RETRIEVAL                                │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐  │
│  │   Vector Search  │  │   Full-Text      │  │  Metadata    │  │
│  │   (Semantic)     │  │   Search         │  │  Filters     │  │
│  │                  │  │                  │  │              │  │
│  │  Chroma/Pinecone │  │  PostgreSQL FTS  │  │  doc_type,   │  │
│  │  + OpenAI embed  │  │  или Elasticsearch│  │  date, party │  │
│  └────────┬─────────┘  └────────┬─────────┘  └──────┬───────┘  │
│           │                     │                   │          │
│           └──────────┬──────────┴───────────────────┘          │
│                      ▼                                          │
│              Hybrid Fusion + Reranking                         │
│              (Cross-Encoder или Cohere Rerank)                 │
│                      │                                          │
│                      ▼                                          │
│              Top-K документов (5-15)                           │
└─────────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                       GENERATION                                │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ System Prompt:                                           │   │
│  │ "Ты - арбитражный юрист-аналитик..."                     │   │
│  ├─────────────────────────────────────────────────────────┤   │
│  │ Context:                                                 │   │
│  │ [Case Summary - всегда]                                  │   │
│  │ [Retrieved Chunks - топ релевантные]                     │   │
│  ├─────────────────────────────────────────────────────────┤   │
│  │ User Question:                                           │   │
│  │ "Какие доводы ответчика по сроку давности?"              │   │
│  └─────────────────────────────────────────────────────────┘   │
│                             │                                   │
│                             ▼                                   │
│                     Claude / GPT-4                              │
│                             │                                   │
│                             ▼                                   │
│  "Ответчик заявил о пропуске срока в отзыве от 15.03.2024.     │
│   Основные аргументы: 1) ... 2) ... [Источник: doc_id]"        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 5.2 Умный chunking для юридических документов

```python
def smart_chunk_legal_document(doc: EnrichedDocument) -> list[Chunk]:
    """Разбивка по смысловым блокам, а не по символам."""

    chunks = []

    # 1. Приоритет резолютивной части
    if "резолютивная" in doc.sections:
        chunks.append(Chunk(
            text=doc.sections["резолютивная"],
            metadata={
                "doc_id": doc.doc_id,
                "section": "резолютивная",
                "priority": 1.0,  # Высший приоритет
            }
        ))

    # 2. Мотивировочная часть - разбить по абзацам
    if "мотивировочная" in doc.sections:
        paragraphs = doc.sections["мотивировочная"].split("\n\n")
        for i, para in enumerate(paragraphs):
            if len(para) > 100:
                chunks.append(Chunk(
                    text=para,
                    metadata={
                        "doc_id": doc.doc_id,
                        "section": "мотивировочная",
                        "paragraph": i,
                        "priority": 0.8,
                    }
                ))

    # 3. Описательная часть - меньший приоритет
    if "описательная" in doc.sections:
        # Разбить на чанки по ~1000 символов
        text = doc.sections["описательная"]
        for i, chunk_text in enumerate(split_with_overlap(text, 1000, 200)):
            chunks.append(Chunk(
                text=chunk_text,
                metadata={
                    "doc_id": doc.doc_id,
                    "section": "описательная",
                    "chunk_index": i,
                    "priority": 0.5,
                }
            ))

    return chunks
```

### 5.3 Query Expansion для юридических терминов

```python
LEGAL_SYNONYMS = {
    # Процессуальные термины
    "срок давности": ["исковая давность", "пропуск срока", "истечение срока", "давностный срок"],
    "подсудность": ["компетенция суда", "подведомственность"],
    "обеспечительные меры": ["обеспечение иска", "арест имущества", "запрет действий"],

    # Материальные термины
    "банкротство": ["несостоятельность", "конкурсное производство", "финансовая несостоятельность"],
    "договор": ["контракт", "соглашение", "сделка"],
    "убытки": ["ущерб", "вред", "потери", "упущенная выгода"],
    "неустойка": ["штраф", "пени", "штрафные санкции"],

    # Стороны
    "истец": ["заявитель", "взыскатель", "кредитор"],
    "ответчик": ["должник", "обязанное лицо"],
}

def expand_legal_query(query: str) -> list[str]:
    """Расширить запрос юридическими синонимами."""
    queries = [query]
    query_lower = query.lower()

    for term, synonyms in LEGAL_SYNONYMS.items():
        if term in query_lower:
            for syn in synonyms:
                expanded = query_lower.replace(term, syn)
                if expanded not in queries:
                    queries.append(expanded)

    return queries[:5]  # Максимум 5 вариантов
```

### 5.4 Two-stage retrieval

```python
async def two_stage_answer(rag: LegalRAG, question: str) -> str:
    """Двухэтапный поиск: сначала выбор документов, потом анализ."""

    # Этап 1: LLM выбирает релевантные документы из индекса
    doc_index = rag.get_document_index()  # Список summaries всех документов

    selection_prompt = f"""Вопрос пользователя: {question}

Доступные документы:
{format_doc_index(doc_index)}

Какие документы нужно изучить для ответа? Выбери до 10 наиболее релевантных.
Верни только doc_id через запятую."""

    selected_ids = await rag.llm.complete(selection_prompt)
    selected_ids = parse_doc_ids(selected_ids)

    # Этап 2: Загрузить выбранные документы и ответить
    selected_docs = rag.get_documents_by_ids(selected_ids)

    answer_prompt = f"""## Дело
{rag.case_summary}

## Выбранные документы
{format_full_documents(selected_docs)}

## Вопрос
{question}

Ответь на основе предоставленных документов. Ссылайся на конкретные источники."""

    return await rag.llm.complete(answer_prompt)
```

### 5.5 Полная реализация RAG-модуля

```python
# src/rag/legal_rag.py
"""
RAG система для анализа арбитражных дел.
"""

import json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import chromadb
from chromadb.utils import embedding_functions
import anthropic


@dataclass
class Chunk:
    id: str
    text: str
    metadata: dict


@dataclass
class SearchResult:
    chunk: Chunk
    score: float


class LegalRAG:
    """RAG система для юридических документов."""

    def __init__(self, case_dir: Path, db_path: Path = Path("./chroma_db")):
        self.case_dir = case_dir
        self.case_id = case_dir.name

        # Vector store
        self.embedding_fn = embedding_functions.OpenAIEmbeddingFunction(
            model_name="text-embedding-3-small"
        )
        self.client = chromadb.PersistentClient(path=str(db_path))
        self.collection = self.client.get_or_create_collection(
            name=f"case_{self.case_id}",
            embedding_function=self.embedding_fn
        )

        # LLM
        self.llm = anthropic.Anthropic()

        # Case summary
        self.case_summary = self._load_case_summary()

    def index_case(self) -> int:
        """Индексировать все документы дела."""
        docs_dir = self.case_dir / "documents"
        chunks = []

        for doc_file in docs_dir.glob("*.json"):
            doc = json.loads(doc_file.read_text())
            chunks.extend(self._chunk_document(doc))

        if chunks:
            self.collection.add(
                ids=[c.id for c in chunks],
                documents=[c.text for c in chunks],
                metadatas=[c.metadata for c in chunks]
            )

        return len(chunks)

    def search(self, query: str, top_k: int = 10) -> list[SearchResult]:
        """Семантический поиск."""
        results = self.collection.query(
            query_texts=[query],
            n_results=top_k,
            include=["documents", "metadatas", "distances"]
        )

        return [
            SearchResult(
                chunk=Chunk(
                    id=results["ids"][0][i],
                    text=results["documents"][0][i],
                    metadata=results["metadatas"][0][i]
                ),
                score=1 - results["distances"][0][i]
            )
            for i in range(len(results["ids"][0]))
        ]

    def answer(self, question: str) -> str:
        """Ответить на вопрос о деле."""
        # Расширение запроса
        queries = expand_legal_query(question)

        # Поиск
        all_results = []
        for q in queries:
            all_results.extend(self.search(q, top_k=5))

        # Дедупликация и сортировка
        seen = set()
        unique_results = []
        for r in sorted(all_results, key=lambda x: -x.score):
            if r.chunk.id not in seen:
                seen.add(r.chunk.id)
                unique_results.append(r)

        # Генерация ответа
        context = self._format_context(unique_results[:10])

        response = self.llm.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system="Ты - опытный арбитражный юрист-аналитик.",
            messages=[{
                "role": "user",
                "content": f"""## Дело
{self.case_summary}

## Найденные фрагменты
{context}

## Вопрос
{question}

Ответь на основе предоставленных фрагментов. Ссылайся на источники."""
            }]
        )

        return response.content[0].text

    def find_weaknesses(self, party: str = "истец") -> str:
        """Найти слабые места стороны."""
        queries = [
            f"слабые места {party}",
            f"возражения против {party}",
            f"недостатки доказательств {party}",
        ]

        results = []
        for q in queries:
            results.extend(self.search(q, top_k=5))

        context = self._format_context(results[:15])

        response = self.llm.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": FIND_WEAKNESSES_PROMPT.format(
                    party=party,
                    case_summary=self.case_summary,
                    documents=context
                )
            }]
        )

        return response.content[0].text
```

---

## 6. Целевая архитектура

### 6.1 Структура проекта

```
kad-parser/
├── src/
│   ├── __init__.py
│   │
│   ├── models/                    # Pydantic модели
│   │   ├── __init__.py
│   │   ├── document.py            # DocumentMeta, EnrichedDocument
│   │   ├── case.py                # CaseInfo, CaseSummary
│   │   ├── entities.py            # Money, DateEntity, LegalReference
│   │   └── progress.py            # Progress tracking
│   │
│   ├── browser/                   # Playwright автоматизация
│   │   ├── __init__.py
│   │   ├── context.py             # Browser setup, stealth mode
│   │   ├── navigation.py          # Search, navigate, pagination
│   │   └── download.py            # PDF download with interceptor
│   │
│   ├── parsers/                   # HTML парсеры
│   │   ├── __init__.py
│   │   ├── court_acts.py          # Вкладка "Судебные акты"
│   │   ├── cards.py               # Вкладка "Карточки"
│   │   ├── electronic_case.py     # Вкладка "Электронное дело"
│   │   └── document_parser.py     # Извлечение метаданных из HTML
│   │
│   ├── enrichment/                # Обогащение данных
│   │   ├── __init__.py
│   │   ├── classifier.py          # Классификация документов
│   │   ├── extractor.py           # Извлечение сущностей
│   │   ├── summarizer.py          # Генерация summary через LLM
│   │   └── section_parser.py      # Разбор на секции
│   │
│   ├── rag/                       # RAG система
│   │   ├── __init__.py
│   │   ├── indexer.py             # Индексация в vector store
│   │   ├── retriever.py           # Поиск (semantic + keyword)
│   │   ├── generator.py           # Генерация ответов
│   │   └── prompts.py             # Промпты для анализа
│   │
│   ├── storage/                   # Persistence
│   │   ├── __init__.py
│   │   ├── filesystem.py          # JSON/PDF файлы
│   │   ├── database.py            # SQLite/PostgreSQL
│   │   └── vector_store.py        # Chroma/Pinecone wrapper
│   │
│   ├── utils/                     # Утилиты
│   │   ├── __init__.py
│   │   ├── delays.py              # Human-like delays
│   │   ├── extractors.py          # GUID, date extraction
│   │   └── text.py                # Text processing
│   │
│   ├── config.py                  # Pydantic Settings
│   ├── cli.py                     # Typer CLI
│   └── api.py                     # FastAPI (опционально)
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                # Fixtures
│   ├── test_parsers.py
│   ├── test_extractors.py
│   ├── test_rag.py
│   └── fixtures/                  # Тестовые данные
│       └── sample_case/
│
├── scripts/
│   ├── migrate.py                 # Миграция данных
│   └── benchmark.py               # Бенчмарки
│
├── pyproject.toml
├── requirements.txt
├── .env.example
├── Dockerfile
└── README.md
```

### 6.2 Pipeline обработки дела

```
┌─────────────────────────────────────────────────────────────────┐
│                      PROCESSING PIPELINE                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  STAGE 1: SCRAPING                                              │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Input: Номер дела (А60-21280/2023)                     │   │
│  │                                                          │   │
│  │  1. Search by case number                                │   │
│  │  2. Navigate to case card                                │   │
│  │  3. Parse all tabs (court_acts, cards, electronic_case)  │   │
│  │  4. Download PDFs via response interceptor               │   │
│  │  5. Extract text with PyMuPDF                            │   │
│  │                                                          │   │
│  │  Output: Raw JSON + PDF files                            │   │
│  └─────────────────────────────────────────────────────────┘   │
│                             │                                   │
│                             ▼                                   │
│  STAGE 2: ENRICHMENT                                            │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  1. Classify documents (LLM)                             │   │
│  │     CLAIM, RESPONSE, RULING, EVIDENCE, etc.              │   │
│  │                                                          │   │
│  │  2. Extract sections (regex + heuristics)                │   │
│  │     резолютивная, мотивировочная, описательная           │   │
│  │                                                          │   │
│  │  3. Extract entities (NER + regex)                       │   │
│  │     суммы, даты, статьи законов, ссылки на практику      │   │
│  │                                                          │   │
│  │  4. Generate summaries (LLM)                             │   │
│  │     2-3 предложения на документ                          │   │
│  │                                                          │   │
│  │  5. Build document graph                                 │   │
│  │     связи между документами                              │   │
│  │                                                          │   │
│  │  Output: Enriched JSON                                   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                             │                                   │
│                             ▼                                   │
│  STAGE 3: INDEXING                                              │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  1. Smart chunking (by sections, not by size)            │   │
│  │  2. Generate embeddings (OpenAI text-embedding-3-small)  │   │
│  │  3. Store in vector DB (Chroma / Pinecone)               │   │
│  │  4. Build full-text index (PostgreSQL FTS)               │   │
│  │  5. Generate case summary (LLM)                          │   │
│  │                                                          │   │
│  │  Output: Indexed case ready for RAG                      │   │
│  └─────────────────────────────────────────────────────────┘   │
│                             │                                   │
│                             ▼                                   │
│  STAGE 4: ANALYSIS (on demand)                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  • Answer questions about the case                       │   │
│  │  • Find weaknesses of parties                            │   │
│  │  • Generate timeline                                     │   │
│  │  • Compare with similar cases                            │   │
│  │  • Recommend strategy                                    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 6.3 API endpoints (FastAPI)

```python
# src/api.py

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="KAD Parser API")


class ScrapeRequest(BaseModel):
    case_number: str  # "А60-21280/2023"
    enrich: bool = True
    index: bool = True


class QuestionRequest(BaseModel):
    case_id: str
    question: str


class WeaknessRequest(BaseModel):
    case_id: str
    party: str = "истец"


@app.post("/cases/scrape")
async def scrape_case(request: ScrapeRequest):
    """Спарсить дело с kad.arbitr.ru."""
    # 1. Scrape
    case_dir = await scraper.scrape(request.case_number)

    # 2. Enrich (optional)
    if request.enrich:
        await enricher.enrich(case_dir)

    # 3. Index (optional)
    if request.index:
        await indexer.index(case_dir)

    return {"case_id": case_dir.name, "status": "completed"}


@app.post("/cases/{case_id}/ask")
async def ask_question(case_id: str, request: QuestionRequest):
    """Задать вопрос о деле."""
    rag = LegalRAG(get_case_dir(case_id))
    answer = await rag.answer(request.question)
    return {"answer": answer}


@app.post("/cases/{case_id}/weaknesses")
async def find_weaknesses(case_id: str, request: WeaknessRequest):
    """Найти слабые места стороны."""
    rag = LegalRAG(get_case_dir(case_id))
    analysis = await rag.find_weaknesses(request.party)
    return {"analysis": analysis}


@app.get("/cases/{case_id}/timeline")
async def get_timeline(case_id: str):
    """Получить хронологию дела."""
    rag = LegalRAG(get_case_dir(case_id))
    timeline = await rag.extract_timeline()
    return {"timeline": timeline}


@app.get("/cases/{case_id}/summary")
async def get_summary(case_id: str):
    """Получить summary дела."""
    case_dir = get_case_dir(case_id)
    summary = (case_dir / "summary.md").read_text()
    return {"summary": summary}
```

---

## 7. План реализации

### Phase 1: Стабилизация (1-2 недели)

| Задача | Приоритет | Статус |
|--------|-----------|--------|
| Исправить refresh сессии после break | P0 | ⬜ |
| Заменить пустые except на конкретные | P0 | ⬜ |
| Добавить graceful shutdown | P0 | ⬜ |
| Разбить на модули (src/) | P1 | ⬜ |
| Вынести конфиг в .env | P1 | ⬜ |
| Добавить базовые тесты | P2 | ⬜ |

### Phase 2: Обогащение данных (2-3 недели)

| Задача | Приоритет | Статус |
|--------|-----------|--------|
| Классификатор документов (LLM) | P1 | ⬜ |
| Парсер секций (regex) | P1 | ⬜ |
| Экстрактор сущностей | P1 | ⬜ |
| Генератор summary | P2 | ⬜ |
| Построение графа связей | P2 | ⬜ |

### Phase 3: RAG система (2-3 недели)

| Задача | Приоритет | Статус |
|--------|-----------|--------|
| Базовый RAG (Chroma + Claude) | P0 | ⬜ |
| Умный chunking по секциям | P1 | ⬜ |
| Query expansion для юр. терминов | P1 | ⬜ |
| Hybrid search (semantic + keyword) | P2 | ⬜ |
| Reranking (Cross-Encoder) | P3 | ⬜ |

### Phase 4: Аналитика (2-3 недели)

| Задача | Приоритет | Статус |
|--------|-----------|--------|
| Анализ слабых мест | P0 | ⬜ |
| Извлечение хронологии | P1 | ⬜ |
| Сравнение с практикой | P2 | ⬜ |
| Рекомендации по стратегии | P2 | ⬜ |

### Phase 5: Продакшен (2-4 недели)

| Задача | Приоритет | Статус |
|--------|-----------|--------|
| FastAPI + endpoints | P1 | ⬜ |
| PostgreSQL + pgvector | P1 | ⬜ |
| Docker compose | P2 | ⬜ |
| Мониторинг и логирование | P2 | ⬜ |
| Rate limiting и очереди | P2 | ⬜ |

---

## Приложения

### A. Зависимости

```txt
# requirements.txt

# Core
playwright>=1.40.0
playwright-stealth>=1.0.6
pymupdf>=1.23.0
pydantic>=2.0.0
pydantic-settings>=2.0.0

# RAG
chromadb>=0.4.0
openai>=1.0.0
anthropic>=0.18.0

# Optional: Reranking
sentence-transformers>=2.2.0

# API
fastapi>=0.100.0
uvicorn>=0.23.0

# Database
sqlalchemy>=2.0.0
asyncpg>=0.28.0

# Utilities
aiofiles>=23.0.0
typer>=0.9.0
rich>=13.0.0

# Testing
pytest>=7.0.0
pytest-asyncio>=0.21.0
```

### B. Переменные окружения

```bash
# .env.example

# KAD Parser
KAD_BASE_URL=https://kad.arbitr.ru/
KAD_HEADLESS=true
KAD_SLOW_MO=100
KAD_DELAY_DOCS_BASE=3.0
KAD_DELAY_DOCS_JITTER=2.0

# LLM
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...

# Database
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/kadparser

# Vector Store
CHROMA_PERSIST_DIR=./chroma_db
# или
PINECONE_API_KEY=...
PINECONE_INDEX=kad-cases

# Logging
LOG_LEVEL=INFO
```

### C. Docker

```dockerfile
# Dockerfile

FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install firefox

# Copy source
COPY src/ ./src/
COPY scripts/ ./scripts/

# Environment
ENV PYTHONPATH=/app
ENV KAD_HEADLESS=true

CMD ["python", "-m", "src.cli"]
```

```yaml
# docker-compose.yml

version: '3.8'

services:
  parser:
    build: .
    environment:
      - DATABASE_URL=postgresql+asyncpg://postgres:postgres@db/kadparser
      - CHROMA_PERSIST_DIR=/data/chroma
    volumes:
      - ./data:/data
      - ./output:/output
    depends_on:
      - db

  db:
    image: pgvector/pgvector:pg16
    environment:
      - POSTGRES_USER=postgres
      - POSTGRES_PASSWORD=postgres
      - POSTGRES_DB=kadparser
    volumes:
      - pgdata:/var/lib/postgresql/data

  api:
    build: .
    command: uvicorn src.api:app --host 0.0.0.0 --port 8000
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql+asyncpg://postgres:postgres@db/kadparser
    depends_on:
      - db
      - parser

volumes:
  pgdata:
```

---

*Документ создан: 2024-12-14*
*Последнее обновление: 2024-12-14*
