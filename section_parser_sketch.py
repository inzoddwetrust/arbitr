"""
Скетч структурного парсера судебных актов
==========================================

Паттерны обнаружены на реальных документах дела А60-21280/2023 (290 документов).

Типичная структура судебного акта:
1. ШАПКА (header) - реквизиты суда, номер дела, дата
2. НАИМЕНОВАНИЕ - тип документа (ОПРЕДЕЛЕНИЕ, ПОСТАНОВЛЕНИЕ, РЕШЕНИЕ)
3. ВВОДНАЯ ЧАСТЬ - состав суда, участники, предмет
4. ОПИСАТЕЛЬНАЯ/МОТИВИРОВОЧНАЯ - "УСТАНОВИЛ:" -> фактические обстоятельства и мотивы
5. РЕЗОЛЮТИВНАЯ ЧАСТЬ - "ОПРЕДЕЛИЛ:" / "РЕШИЛ:" / "ПОСТАНОВИЛ:" -> выводы суда
6. ПОДВАЛ - подпись судьи, ЭЦП

Выявленные паттерны маркеров секций (с вариациями):
- УСТАНОВИЛ / У С Т А Н О В И Л / установил
- ОПРЕДЕЛИЛ / О П Р Е Д Е Л Е Н И Е / определил
- ПОСТАНОВИЛ / П О С Т А Н О В И Л / постановил
- РЕШИЛ / Р Е Ш И Л / решил
"""

import re
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class SectionType(Enum):
    HEADER = "header"           # Шапка с реквизитами
    TITLE = "title"             # Название документа
    INTRO = "intro"             # Вводная часть (состав суда, участники)
    ESTABLISHED = "established" # УСТАНОВИЛ - факты и обстоятельства
    RULING = "ruling"           # ОПРЕДЕЛИЛ/РЕШИЛ/ПОСТАНОВИЛ - резолюция
    FOOTER = "footer"           # Подпись, ЭЦП


@dataclass
class Section:
    type: SectionType
    text: str
    start: int
    end: int
    marker: Optional[str] = None  # Какой маркер использовался


@dataclass
class ParsedDocument:
    doc_id: str
    doc_type: str
    sections: list[Section] = field(default_factory=list)
    raw_text: str = ""

    def get_section(self, section_type: SectionType) -> Optional[Section]:
        """Получить секцию по типу"""
        for s in self.sections:
            if s.type == section_type:
                return s
        return None

    @property
    def facts(self) -> str:
        """Фактические обстоятельства (УСТАНОВИЛ)"""
        s = self.get_section(SectionType.ESTABLISHED)
        return s.text if s else ""

    @property
    def ruling(self) -> str:
        """Резолютивная часть (ОПРЕДЕЛИЛ/РЕШИЛ/ПОСТАНОВИЛ)"""
        s = self.get_section(SectionType.RULING)
        return s.text if s else ""


class LegalDocumentParser:
    """
    Структурный парсер судебных актов арбитражных судов РФ.

    Поддерживает:
    - Определения (Opredelenie)
    - Постановления (Postanovlenie)
    - Решения (Reshenie)
    """

    # Паттерны маркеров секций (регистронезависимые, с вариантом разрядки)
    SECTION_PATTERNS = {
        'established': [
            # Стандартный
            r'(?:арбитражный\s+суд\s+)?[уУ]\s*[сС]\s*[тТ]\s*[аА]\s*[нН]\s*[оО]\s*[вВ]\s*[иИ]\s*[лЛ]\s*:?',
            # Просто УСТАНОВИЛ:
            r'\bУСТАНОВИЛ\s*:',
            r'\bустановил\s*:',
        ],
        'ruling_opredelenie': [
            # О П Р Е Д Е Л И Л
            r'[оО]\s*[пП]\s*[рР]\s*[еЕ]\s*[дД]\s*[еЕ]\s*[лЛ]\s*[иИ]\s*[лЛ]\s*:?',
            r'\bОПРЕДЕЛИЛ\s*:',
            r'\bопределил\s*:',
        ],
        'ruling_reshenie': [
            r'[рР]\s*[еЕ]\s*[шШ]\s*[иИ]\s*[лЛ]\s*:?',
            r'\bРЕШИЛ\s*:',
            r'\bрешил\s*:',
        ],
        'ruling_postanovlenie': [
            # П О С Т А Н О В И Л (только как маркер секции, не внутри текста)
            r'(?:апелляционный\s+суд\s+)?[пП]\s*[оО]\s*[сС]\s*[тТ]\s*[аА]\s*[нН]\s*[оО]\s*[вВ]\s*[иИ]\s*[лЛ]\s*:',
            r'\n\s*ПОСТАНОВИЛ\s*:',
            r'\n\s*постановил\s*:',
        ],
    }

    # Паттерны заголовков документов
    DOC_TITLE_PATTERNS = [
        r'О\s*П\s*Р\s*Е\s*Д\s*Е\s*Л\s*Е\s*Н\s*И\s*Е',
        r'П\s*О\s*С\s*Т\s*А\s*Н\s*О\s*В\s*Л\s*Е\s*Н\s*И\s*Е',
        r'Р\s*Е\s*Ш\s*Е\s*Н\s*И\s*Е',
        r'ОПРЕДЕЛЕНИЕ',
        r'ПОСТАНОВЛЕНИЕ',
        r'РЕШЕНИЕ',
    ]

    # Паттерны ЭЦП (начало footer)
    SIGNATURE_PATTERNS = [
        r'Электронная подпись действительна',
        r'Данные ЭП:',
        r'Судья\s+[\w\s\.]+$',
    ]

    def __init__(self):
        # Компилируем паттерны
        self._compiled_patterns = {}
        for key, patterns in self.SECTION_PATTERNS.items():
            self._compiled_patterns[key] = [
                re.compile(p, re.IGNORECASE | re.MULTILINE)
                for p in patterns
            ]

    def find_marker(self, text: str, pattern_key: str) -> Optional[tuple[int, int, str]]:
        """
        Найти маркер секции в тексте.

        Returns:
            (start, end, matched_text) или None
        """
        best_match = None

        for pattern in self._compiled_patterns.get(pattern_key, []):
            match = pattern.search(text)
            if match:
                # Берём первое вхождение (ближе к началу = приоритетнее)
                if best_match is None or match.start() < best_match[0]:
                    best_match = (match.start(), match.end(), match.group())

        return best_match

    def find_all_ruling_markers(self, text: str) -> list[tuple[int, int, str, str]]:
        """
        Найти все маркеры резолютивной части.

        Returns:
            List of (start, end, matched_text, marker_type)
        """
        results = []

        for marker_type in ['ruling_opredelenie', 'ruling_reshenie', 'ruling_postanovlenie']:
            for pattern in self._compiled_patterns.get(marker_type, []):
                for match in pattern.finditer(text):
                    results.append((match.start(), match.end(), match.group(), marker_type))

        # Сортируем по позиции
        results.sort(key=lambda x: x[0])
        return results

    def find_document_title(self, text: str) -> Optional[tuple[int, int, str]]:
        """Найти заголовок документа (ОПРЕДЕЛЕНИЕ, ПОСТАНОВЛЕНИЕ, etc.)"""
        for pattern_str in self.DOC_TITLE_PATTERNS:
            pattern = re.compile(pattern_str, re.IGNORECASE)
            match = pattern.search(text[:3000])  # Ищем только в начале
            if match:
                return (match.start(), match.end(), match.group())
        return None

    def find_signature(self, text: str) -> Optional[int]:
        """Найти начало подписи/ЭЦП"""
        for pattern_str in self.SIGNATURE_PATTERNS:
            pattern = re.compile(pattern_str, re.IGNORECASE | re.MULTILINE)
            match = pattern.search(text)
            if match:
                return match.start()
        return None

    def parse(self, text: str, doc_id: str = "", doc_type: str = "") -> ParsedDocument:
        """
        Распарсить судебный акт на секции.

        Args:
            text: Текст документа
            doc_id: ID документа
            doc_type: Тип документа (Opredelenie, Postanovlenie, etc.)

        Returns:
            ParsedDocument с выделенными секциями
        """
        result = ParsedDocument(
            doc_id=doc_id,
            doc_type=doc_type,
            raw_text=text,
            sections=[]
        )

        # 1. Найти заголовок документа
        title_match = self.find_document_title(text)
        title_end = 0

        if title_match:
            # Header - всё до заголовка
            if title_match[0] > 50:  # Есть что-то до заголовка
                result.sections.append(Section(
                    type=SectionType.HEADER,
                    text=text[:title_match[0]].strip(),
                    start=0,
                    end=title_match[0]
                ))

            # Title
            result.sections.append(Section(
                type=SectionType.TITLE,
                text=title_match[2],
                start=title_match[0],
                end=title_match[1],
                marker=title_match[2]
            ))
            title_end = title_match[1]

        # 2. Найти УСТАНОВИЛ
        established_match = self.find_marker(text, 'established')

        # 3. Найти резолютивную часть (ОПРЕДЕЛИЛ/РЕШИЛ/ПОСТАНОВИЛ)
        ruling_markers = self.find_all_ruling_markers(text)

        # Фильтруем маркеры: берём только те, что после УСТАНОВИЛ (если есть)
        # и являются реальными маркерами секций (не упоминания в тексте)
        main_ruling = None
        if ruling_markers:
            for marker in ruling_markers:
                # Если маркер идёт после установил и перед подписью - это наш
                if established_match and marker[0] > established_match[1]:
                    main_ruling = marker
                    break
            # Если УСТАНОВИЛ нет, берём первый ruling
            if main_ruling is None:
                main_ruling = ruling_markers[0]

        # 4. Найти подпись (footer)
        signature_start = self.find_signature(text)

        # 5. Формируем секции

        # INTRO: от title до УСТАНОВИЛ (или до ruling если нет УСТАНОВИЛ)
        intro_start = title_end
        intro_end = None

        if established_match:
            intro_end = established_match[0]
        elif main_ruling:
            intro_end = main_ruling[0]

        if intro_end and intro_end > intro_start:
            result.sections.append(Section(
                type=SectionType.INTRO,
                text=text[intro_start:intro_end].strip(),
                start=intro_start,
                end=intro_end
            ))

        # ESTABLISHED: от маркера до ruling (или до конца/подписи)
        if established_match:
            est_start = established_match[1]  # После маркера
            est_end = main_ruling[0] if main_ruling else (signature_start or len(text))

            result.sections.append(Section(
                type=SectionType.ESTABLISHED,
                text=text[est_start:est_end].strip(),
                start=est_start,
                end=est_end,
                marker=established_match[2]
            ))

        # RULING: от маркера до подписи/конца
        if main_ruling:
            ruling_start = main_ruling[1]  # После маркера
            ruling_end = signature_start if signature_start else len(text)

            result.sections.append(Section(
                type=SectionType.RULING,
                text=text[ruling_start:ruling_end].strip(),
                start=ruling_start,
                end=ruling_end,
                marker=main_ruling[2]
            ))

        # FOOTER: от подписи до конца
        if signature_start:
            result.sections.append(Section(
                type=SectionType.FOOTER,
                text=text[signature_start:].strip(),
                start=signature_start,
                end=len(text)
            ))

        return result


class ContentClassifier:
    """
    Классификатор контента секции УСТАНОВИЛ.

    Выявляет ключевые темы и правовые основания в тексте.
    """

    # Паттерны для классификации контента
    TOPIC_PATTERNS = {
        'bankruptcy_intro': [
            r'признан\w* несостоятельным',
            r'банкрот',
            r'процедур\w* реализаци\w* имущества',
            r'финансов\w+ управляющ\w+',
            r'конкурсн\w+ управляющ\w+',
        ],
        'transaction_challenge': [
            r'оспарива\w+ сделк',
            r'недействительн\w+ сделк',
            r'признан\w+ недействительн',
            r'платеж\w+ в пользу',
            r'перечисл\w+ денежн',
        ],
        'creditor_claims': [
            r'требовани\w+ кредитор',
            r'реестр\w* требований',
            r'включ\w+ в реестр',
            r'задолженност\w+',
        ],
        'affiliates': [
            r'аффилиро\w+',
            r'заинтересован\w+ лиц',
            r'группа компаний',
            r'взаимосвязан\w+',
            r'контролирующ\w+ лиц',
        ],
        'fraud_indicators': [
            r'злоупотреблен\w+ прав',
            r'вывод\w* актив',
            r'причинен\w+ вред',
            r'ущерб кредитор',
            r'недобросовестн',
            r'мнимая сделка',
            r'притворная сделка',
        ],
        'procedural': [
            r'срок исковой давности',
            r'пропущен\w* срок',
            r'оставить без движения',
            r'отложить заседание',
            r'назначить экспертизу',
        ],
    }

    # Статьи законов
    LAW_PATTERNS = {
        'bankruptcy_law': [
            (r'стать\w* (\d+(?:\.\d+)?)\s*(?:Федерального\s+)?[Зз]акона.*о несостоятельности',
             'ФЗ о банкротстве'),
            (r'стать\w* (\d+(?:\.\d+)?)\s*[Зз]акона о банкротстве',
             'ФЗ о банкротстве'),
        ],
        'civil_code': [
            (r'стать\w* (\d+(?:\.\d+)?)\s*(?:Гражданского\s+)?[Кк]одекса',
             'ГК РФ'),
            (r'стать\w* (\d+)\s*ГК\s*(?:РФ)?',
             'ГК РФ'),
        ],
        'procedure_code': [
            (r'стать\w* (\d+(?:\.\d+)?)\s*(?:Арбитражного\s+процессуального\s+)?[Кк]одекса',
             'АПК РФ'),
            (r'стать\w* (\d+)\s*АПК\s*(?:РФ)?',
             'АПК РФ'),
        ],
    }

    # Паттерны для извлечения сущностей
    ENTITY_PATTERNS = {
        'money': [
            r'(\d[\d\s]*(?:\d))\s*руб',
            r'(\d[\d\s]*(?:,\d+)?)\s*(?:руб|тыс|млн)',
            r'сумм\w*\s+(\d[\d\s]*)\s*руб',
        ],
        'dates': [
            r'(\d{2}\.\d{2}\.\d{4})',
            r'(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})',
        ],
        'case_numbers': [
            r'дел[оау]?\s*[№N]?\s*(А\d+-\d+/\d{4})',
            r'№\s*(А\d+-\d+/\d{4})',
        ],
        'inn': [
            r'ИНН\s*(\d{10,12})',
        ],
        'ogrn': [
            r'ОГРН(?:ИП)?\s*(\d{13,15})',
        ],
    }

    def __init__(self):
        self._compiled_topics = {}
        for topic, patterns in self.TOPIC_PATTERNS.items():
            self._compiled_topics[topic] = [
                re.compile(p, re.IGNORECASE) for p in patterns
            ]

    def classify_topics(self, text: str) -> dict[str, int]:
        """
        Определить темы в тексте.

        Returns:
            Dict[topic_name -> count of matches]
        """
        results = {}
        for topic, patterns in self._compiled_topics.items():
            count = 0
            for pattern in patterns:
                matches = pattern.findall(text)
                count += len(matches)
            if count > 0:
                results[topic] = count
        return results

    def extract_law_references(self, text: str) -> list[dict]:
        """
        Извлечь ссылки на статьи законов.

        Returns:
            List of {article, law_name, context}
        """
        results = []
        for law_type, patterns in self.LAW_PATTERNS.items():
            for pattern_str, law_name in patterns:
                pattern = re.compile(pattern_str, re.IGNORECASE)
                for match in pattern.finditer(text):
                    article = match.group(1)
                    # Контекст: 50 символов до и после
                    start = max(0, match.start() - 50)
                    end = min(len(text), match.end() + 50)
                    context = text[start:end].replace('\n', ' ')

                    results.append({
                        'article': article,
                        'law': law_name,
                        'context': context,
                    })
        return results

    def extract_entities(self, text: str) -> dict[str, list]:
        """
        Извлечь именованные сущности.

        Returns:
            Dict[entity_type -> list of values]
        """
        results = {}
        for entity_type, patterns in self.ENTITY_PATTERNS.items():
            values = set()
            for pattern_str in patterns:
                pattern = re.compile(pattern_str, re.IGNORECASE)
                for match in pattern.finditer(text):
                    # Для дат склеиваем группы
                    if entity_type == 'dates' and len(match.groups()) > 1:
                        values.add(' '.join(g for g in match.groups() if g))
                    else:
                        values.add(match.group(1))
            if values:
                results[entity_type] = sorted(values)
        return results

    def analyze(self, text: str) -> dict:
        """Полный анализ текста секции"""
        return {
            'topics': self.classify_topics(text),
            'laws': self.extract_law_references(text),
            'entities': self.extract_entities(text),
        }


class ChunkBoundaryFinder:
    """
    Находит границы для интеллектуального разбиения текста.

    Используется в связке с Late Chunking (см. ARCHITECTURE.md, раздел 5.3).
    Идея: найти семантические границы в тексте, затем применить
    Late Chunking с учетом этих границ.
    """

    # Паттерны границ параграфов
    BOUNDARY_PATTERNS = [
        # Нумерованные пункты в резолюции
        r'\n\s*\d+\.\s+',
        # Подпункты
        r'\n\s*\d+\)\s+',
        # Буквенные пункты
        r'\n\s*[а-яa-z]\)\s+',
        # Абзацы с отступом
        r'\n\s{4,}[А-ЯA-Z]',
        # Двойной перенос (пустая строка)
        r'\n\s*\n',
        # Ключевые слова-переходы
        r'\n\s*(?:Вместе с тем|Между тем|При этом|Однако|Таким образом|Учитывая изложенное)',
        # Ссылки на материалы дела
        r'\n\s*(?:Как следует из|Из материалов дела|Согласно представленным)',
    ]

    def __init__(self, min_chunk_size: int = 300, max_chunk_size: int = 2000):
        self.min_chunk_size = min_chunk_size
        self.max_chunk_size = max_chunk_size
        self._patterns = [re.compile(p) for p in self.BOUNDARY_PATTERNS]

    def find_boundaries(self, text: str) -> list[int]:
        """
        Найти позиции границ в тексте.

        Returns:
            Отсортированный список позиций границ
        """
        boundaries = set([0])  # Начало текста

        for pattern in self._patterns:
            for match in pattern.finditer(text):
                boundaries.add(match.start())

        boundaries.add(len(text))  # Конец текста
        return sorted(boundaries)

    def merge_small_chunks(self, boundaries: list[int], text: str) -> list[int]:
        """
        Объединить слишком маленькие чанки.
        """
        result = [boundaries[0]]

        for i in range(1, len(boundaries)):
            prev = result[-1]
            curr = boundaries[i]
            chunk_size = curr - prev

            if chunk_size < self.min_chunk_size and i < len(boundaries) - 1:
                # Слишком маленький чанк - пропускаем границу
                continue

            result.append(curr)

        return result

    def split_large_chunks(self, boundaries: list[int], text: str) -> list[int]:
        """
        Разбить слишком большие чанки.
        """
        result = []

        for i in range(len(boundaries) - 1):
            start = boundaries[i]
            end = boundaries[i + 1]
            chunk_size = end - start

            result.append(start)

            if chunk_size > self.max_chunk_size:
                # Разбиваем на примерно равные части
                n_parts = (chunk_size // self.max_chunk_size) + 1
                part_size = chunk_size // n_parts

                for j in range(1, n_parts):
                    # Ищем ближайший перенос строки
                    target = start + j * part_size
                    newline = text.find('\n', target)
                    if newline != -1 and newline < end:
                        result.append(newline)

        result.append(boundaries[-1])
        return sorted(set(result))

    def get_chunks(self, text: str) -> list[tuple[int, int, str]]:
        """
        Разбить текст на чанки.

        Returns:
            List of (start, end, chunk_text)
        """
        boundaries = self.find_boundaries(text)
        boundaries = self.merge_small_chunks(boundaries, text)
        boundaries = self.split_large_chunks(boundaries, text)

        chunks = []
        for i in range(len(boundaries) - 1):
            start = boundaries[i]
            end = boundaries[i + 1]
            chunk_text = text[start:end].strip()
            if chunk_text:  # Пропускаем пустые чанки
                chunks.append((start, end, chunk_text))

        return chunks


def analyze_document_structure(doc_path: str):
    """Анализировать структуру документа из JSON-файла"""
    import json

    with open(doc_path) as f:
        doc = json.load(f)

    parser = LegalDocumentParser()
    result = parser.parse(
        text=doc.get('text', ''),
        doc_id=doc.get('doc_id', ''),
        doc_type=doc.get('doc_type', '')
    )

    print(f"\n{'='*60}")
    print(f"Document: {doc.get('filename', 'unknown')}")
    print(f"Type: {doc.get('doc_type', 'unknown')}")
    print(f"Instance: {doc.get('instance_name', 'unknown')}")
    print(f"Char count: {doc.get('char_count', 0)}")
    print(f"{'='*60}")

    for section in result.sections:
        preview = section.text[:200].replace('\n', ' ')
        if len(section.text) > 200:
            preview += "..."

        print(f"\n[{section.type.value.upper()}] ({section.start}-{section.end}, {len(section.text)} chars)")
        if section.marker:
            print(f"  Marker: '{section.marker}'")
        print(f"  Preview: {preview}")

    return result


def analyze_document_full(doc_path: str):
    """
    Полный анализ документа: структура + классификация + сущности.
    """
    import json

    with open(doc_path) as f:
        doc = json.load(f)

    # Парсинг структуры
    parser = LegalDocumentParser()
    parsed = parser.parse(
        text=doc.get('text', ''),
        doc_id=doc.get('doc_id', ''),
        doc_type=doc.get('doc_type', '')
    )

    # Классификация контента
    classifier = ContentClassifier()

    # Чанкинг
    chunker = ChunkBoundaryFinder()

    print(f"\n{'='*70}")
    print(f"ПОЛНЫЙ АНАЛИЗ: {doc.get('filename', 'unknown')}")
    print(f"{'='*70}")

    # Анализ секции УСТАНОВИЛ
    established = parsed.get_section(SectionType.ESTABLISHED)
    if established:
        analysis = classifier.analyze(established.text)

        print(f"\n--- СЕКЦИЯ 'УСТАНОВИЛ' ({len(established.text)} chars) ---")

        print("\nТемы:")
        for topic, count in sorted(analysis['topics'].items(), key=lambda x: -x[1]):
            print(f"  • {topic}: {count} упоминаний")

        print(f"\nСсылки на законы ({len(analysis['laws'])} найдено):")
        seen = set()
        for ref in analysis['laws'][:10]:  # Первые 10
            key = (ref['article'], ref['law'])
            if key not in seen:
                print(f"  • ст. {ref['article']} {ref['law']}")
                seen.add(key)

        print("\nСущности:")
        for entity_type, values in analysis['entities'].items():
            print(f"  {entity_type}: {values[:5]}")  # Первые 5

        # Чанки
        chunks = chunker.get_chunks(established.text)
        print(f"\nЧанки: {len(chunks)} штук")
        for i, (start, end, text) in enumerate(chunks[:3]):
            preview = text[:100].replace('\n', ' ')
            print(f"  [{i+1}] ({end-start} chars): {preview}...")

    # Анализ секции ОПРЕДЕЛИЛ
    ruling = parsed.get_section(SectionType.RULING)
    if ruling:
        print(f"\n--- СЕКЦИЯ 'ОПРЕДЕЛИЛ' ({len(ruling.text)} chars) ---")
        print(f"  {ruling.text[:500].replace(chr(10), ' ')}...")

    return parsed


def analyze_case_statistics(case_path: str):
    """
    Собрать статистику по всем документам дела.
    """
    import json
    import os
    from collections import Counter, defaultdict

    parser = LegalDocumentParser()
    classifier = ContentClassifier()

    stats = {
        'total_docs': 0,
        'docs_with_established': 0,
        'docs_with_ruling': 0,
        'section_sizes': defaultdict(list),
        'topics': Counter(),
        'laws': Counter(),
        'entities': defaultdict(set),
        'by_instance': defaultdict(int),
        'by_doc_type': defaultdict(int),
    }

    # Найти все документы
    for root, dirs, files in os.walk(case_path):
        for f in files:
            if f.endswith('.json') and not f.startswith('instance') and 'documents' not in root:
                path = os.path.join(root, f)
                try:
                    with open(path) as fp:
                        doc = json.load(fp)

                    if not doc.get('text'):
                        continue

                    stats['total_docs'] += 1
                    stats['by_instance'][doc.get('instance_name', 'unknown')] += 1
                    stats['by_doc_type'][doc.get('doc_type', 'unknown')] += 1

                    # Парсинг
                    parsed = parser.parse(
                        text=doc['text'],
                        doc_id=doc.get('doc_id', ''),
                        doc_type=doc.get('doc_type', '')
                    )

                    # Статистика секций
                    for section in parsed.sections:
                        stats['section_sizes'][section.type.value].append(len(section.text))

                    # Анализ УСТАНОВИЛ
                    established = parsed.get_section(SectionType.ESTABLISHED)
                    if established:
                        stats['docs_with_established'] += 1
                        analysis = classifier.analyze(established.text)

                        for topic, count in analysis['topics'].items():
                            stats['topics'][topic] += count

                        for ref in analysis['laws']:
                            stats['laws'][f"ст. {ref['article']} {ref['law']}"] += 1

                        for entity_type, values in analysis['entities'].items():
                            stats['entities'][entity_type].update(values)

                    # Наличие ОПРЕДЕЛИЛ
                    if parsed.get_section(SectionType.RULING):
                        stats['docs_with_ruling'] += 1

                except Exception as e:
                    print(f"Error processing {path}: {e}")

    # Вывод статистики
    print(f"\n{'='*70}")
    print(f"СТАТИСТИКА ДЕЛА: {case_path}")
    print(f"{'='*70}")

    print(f"\nВсего документов: {stats['total_docs']}")
    print(f"С секцией УСТАНОВИЛ: {stats['docs_with_established']} ({100*stats['docs_with_established']/max(1,stats['total_docs']):.1f}%)")
    print(f"С секцией ОПРЕДЕЛИЛ: {stats['docs_with_ruling']} ({100*stats['docs_with_ruling']/max(1,stats['total_docs']):.1f}%)")

    print("\nПо инстанциям:")
    for inst, count in sorted(stats['by_instance'].items()):
        print(f"  {inst}: {count}")

    print("\nПо типам документов:")
    for doc_type, count in sorted(stats['by_doc_type'].items(), key=lambda x: -x[1]):
        print(f"  {doc_type}: {count}")

    print("\nРазмеры секций (средний, chars):")
    for section_type, sizes in stats['section_sizes'].items():
        if sizes:
            avg = sum(sizes) / len(sizes)
            print(f"  {section_type}: {avg:.0f} (min={min(sizes)}, max={max(sizes)}, n={len(sizes)})")

    print("\nТоп-10 тем:")
    for topic, count in stats['topics'].most_common(10):
        print(f"  {topic}: {count}")

    print("\nТоп-10 статей законов:")
    for law, count in stats['laws'].most_common(10):
        print(f"  {law}: {count}")

    print("\nУникальные сущности:")
    for entity_type, values in stats['entities'].items():
        print(f"  {entity_type}: {len(values)} уникальных")

    return stats


# Тест на реальных данных
if __name__ == "__main__":
    import sys
    import os

    # Путь к тестовому кейсу
    TEST_CASE = "/home/user/testcase/output/case_А60-21280-2023"

    # Режимы запуска
    if len(sys.argv) > 1:
        if sys.argv[1] == "--stats":
            # Статистика по всему делу
            analyze_case_statistics(TEST_CASE)
        elif sys.argv[1] == "--full":
            # Полный анализ документа
            if len(sys.argv) > 2:
                analyze_document_full(sys.argv[2])
            else:
                # Пример с большим документом
                analyze_document_full(f"{TEST_CASE}/instances/09_Первая_инстанция_1a3f5923/007_027aff23.json")
        else:
            # Анализ переданных файлов
            for doc_path in sys.argv[1:]:
                if os.path.exists(doc_path):
                    try:
                        analyze_document_structure(doc_path)
                    except Exception as e:
                        print(f"Error parsing {doc_path}: {e}")
                else:
                    print(f"File not found: {doc_path}")
    else:
        # По умолчанию - примеры документов
        test_docs = [
            f"{TEST_CASE}/instances/02_Первая_инстанция_44af3e0d/002_55bf8112.json",
            f"{TEST_CASE}/instances/03_Апелляционная_инстанция_4f302a7f/003_e5f69450.json",
            f"{TEST_CASE}/instances/06_Кассационная_инстанция_bb622209/002_38bd666b.json",
            f"{TEST_CASE}/instances/09_Первая_инстанция_1a3f5923/010_7157c9f2.json",
        ]

        for doc_path in test_docs:
            if os.path.exists(doc_path):
                try:
                    analyze_document_structure(doc_path)
                except Exception as e:
                    print(f"Error parsing {doc_path}: {e}")
            else:
                print(f"File not found: {doc_path}")
