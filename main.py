from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
import anthropic
from supabase import create_client, Client
import httpx
import os
import json
import math
import re
import time
import base64
import hashlib
from collections import defaultdict
from typing import Optional  # використовується для authorization header

app = FastAPI()

# Simple in-memory rate limiter by user token
_rate_limit_store: dict = defaultdict(list)
RATE_LIMIT_REQUESTS = 5
RATE_LIMIT_WINDOW = 60  # seconds

def _rate_limit_key(token: str) -> str:
    """
    Ключ лічильника — user_id з JWT, а не хвіст токена.

    Раніше ключем були останні 16 символів токена. Повторний логін давав новий
    токен, отже новий ключ, отже чистий лічильник — ліміт обходився за секунду
    (BUG-008, кейс 14.3). Підпис тут НЕ перевіряється і не має перевірятись:
    це лише групування запитів. Справжня автентифікація — далі, у
    get_user_profile через Supabase.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        sub = json.loads(base64.urlsafe_b64decode(payload)).get("sub")
        if sub:
            return f"u:{sub}"
    except Exception:
        pass
    # Токен нечитабельний (битий або не JWT) — все одно рахуємо запит,
    # просто в окремому відрі. Такі запити далі отримають 401.
    return "t:" + hashlib.sha256(token.encode()).hexdigest()[:16]


def check_rate_limit(token: str):
    now = time.time()
    key = _rate_limit_key(token)
    timestamps = _rate_limit_store[key]
    # Прибираємо старі запити
    timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    _rate_limit_store[key] = timestamps
    if len(timestamps) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a minute.")
    timestamps.append(now)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SUPABASE_URL = "https://jgmjbwsfseoyympaxjdf.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImpnbWpid3Nmc2VveXltcGF4amRmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODAzNzU5NjksImV4cCI6MjA5NTk1MTk2OX0.jsVFhOoRqUYQtYrL8cG6Z2hqx0wuscogVzRTyG2ofGA"
EDGE_FUNCTION_URL = "https://jgmjbwsfseoyympaxjdf.supabase.co/functions/v1/increment-generations"

PLAN_MODELS = {
    "free": "claude-haiku-4-5-20251001",
    "pro": "claude-sonnet-4-6",
    "founder": "claude-sonnet-4-6",
    "studio": "claude-sonnet-4-6",
}

# Вартість Try again по планах — визначається ТІЛЬКИ на бекенді
PLAN_RETRY_COSTS = {
    "free": 0.5,
    "pro": 0.25,
    "founder": 0.25,
    "studio": 0.1,
}


def _strip_code_fences(raw: str) -> str:
    """
    Прибирає ```json ... ``` навколо відповіді.

    Стара версія робила text.split("```")[1] — при непарній кількості фенсів
    (модель відкрила блок і не закрила, бо обірвалась) це давало IndexError,
    тобто 500 замість зрозумілої помилки.
    """
    text = (raw or "").strip()
    if not text.startswith("```"):
        return text
    text = text[3:]
    if text[:4].lower() == "json":
        text = text[4:]
    closing = text.rfind("```")
    if closing != -1:
        text = text[:closing]
    return text.strip()


def _stream_message(**kwargs):
    """
    Виклик моделі зі стрімом + замір часу.

    Без стріму з'єднання мовчить до кінця генерації, і проміжний проксі рве його
    по таймауту простою — звідси обриви на 5-6 хвилині. Зі стрімом дані течуть
    постійно.

    Системний промпт позначається cache_control: ephemeral. Він однаковий для
    всіх запитів, тож після першого виклику модель бере його з кешу — трохи
    швидше і помітно дешевше.

    Лог: час до першого токена окремо від загального. Якщо перший токен іде
    довго, вузьке місце — вхід (промпт, черга). Якщо перший токен швидко, а
    загальний час великий, вузьке місце — обсяг виводу.
    """
    system = kwargs.get("system")
    if isinstance(system, str):
        kwargs["system"] = [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]

    label = kwargs.pop("_label", "generate")
    started = time.time()
    first_token_at = None

    with client.messages.stream(**kwargs) as stream:
        for _ in stream.text_stream:
            if first_token_at is None:
                first_token_at = time.time()
        message = stream.get_final_message()

    total = time.time() - started
    ttft = (first_token_at - started) if first_token_at else total
    usage = getattr(message, "usage", None)
    print(
        "[timing] %s model=%s ttft=%.1fs total=%.1fs in=%s out=%s cache_read=%s stop=%s"
        % (
            label,
            kwargs.get("model"),
            ttft,
            total,
            getattr(usage, "input_tokens", "?"),
            getattr(usage, "output_tokens", "?"),
            getattr(usage, "cache_read_input_tokens", "?"),
            getattr(message, "stop_reason", "?"),
        ),
        flush=True,
    )
    return message


def enrich_chart(pattern: dict) -> dict:
    """
    Заповнює обчислювані поля чарта на сервері.

    Модель більше не пише round, shape_change, color_name, increases і
    decreases — це економить значну частину виводу, а отже часу. Все воно
    однозначно виводиться з stitch_count і вже розгорнутого symbols.

    Викликати ПІСЛЯ fix_chart_types(): та розгортає компактний запис "26 sc"
    у список окремих петель, а increases/decreases — це індекси саме в
    розгорнутому списку.

    Якщо модель усе-таки прислала ці поля — вони не чіпаються.
    """
    try:
        chart = pattern.get("chart")
        if not isinstance(chart, dict):
            return pattern
        sections = chart.get("sections")
        if not isinstance(sections, list):
            return pattern

        for section in sections:
            if not isinstance(section, dict):
                continue
            section_color = section.get("color_name")
            rounds = section.get("rounds")
            if not isinstance(rounds, list):
                continue
            prev_count = None
            for i, r in enumerate(rounds):
                if not isinstance(r, dict):
                    continue

                if not r.get("round"):
                    r["round"] = i + 1

                if not r.get("color_name") and section_color:
                    r["color_name"] = section_color

                symbols = r.get("symbols")
                symbols = symbols if isinstance(symbols, list) else []

                if "increases" not in r or not isinstance(r.get("increases"), list):
                    r["increases"] = [j for j, s in enumerate(symbols) if str(s).lower() == "inc"]
                if "decreases" not in r or not isinstance(r.get("decreases"), list):
                    r["decreases"] = [j for j, s in enumerate(symbols) if str(s).lower() == "dec"]

                count = r.get("stitch_count")
                count = count if isinstance(count, (int, float)) else None
                if not r.get("shape_change"):
                    if prev_count is None or count is None:
                        r["shape_change"] = "expanding" if r["increases"] else "straight"
                    elif count > prev_count:
                        r["shape_change"] = "expanding"
                    elif count < prev_count:
                        r["shape_change"] = "decreasing"
                    else:
                        r["shape_change"] = "straight"
                if count is not None:
                    prev_count = count

            if not section.get("shape_change"):
                counts = [
                    r.get("stitch_count") for r in rounds
                    if isinstance(r, dict) and isinstance(r.get("stitch_count"), (int, float))
                ]
                if len(counts) >= 2 and counts[-1] > counts[0]:
                    section["shape_change"] = "expanding"
                elif len(counts) >= 2 and counts[-1] < counts[0]:
                    section["shape_change"] = "decreasing"
                else:
                    section["shape_change"] = "straight"
    except Exception:
        pass
    return pattern


def get_user_profile(authorization: str):
    token = authorization.replace("Bearer ", "")
    authed_client: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    authed_client.postgrest.auth(token)
    # BUG-001 (кейс 2.4): на битому чи простроченому JWT get_user кидає виняток
    # бібліотеки, який без обробки перетворювався на 500. Правильна відповідь —
    # 401: клієнт надіслав невалідні дані, сервер справний. Усі Edge Functions
    # цього ж продукту обробляють той самий випадок саме так.
    try:
        user_resp = authed_client.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not user_resp or not user_resp.user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user_resp.user.id
    try:
        profile_resp = authed_client.table("profiles").select("*").eq(
            "user_id", user_id).single().execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Profile not found")
    if not profile_resp or not profile_resp.data:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile_resp.data


def increment_generations(authorization: str, is_retry: bool = False, dry_run: bool = False):
    """
    Викликає Edge Function для безпечного списування генерацій.

    BUG-004 (кейси 3.4-3.6): раніше сюди слався {"amount": ...}, а функція
    increment-generations читає з тіла ТІЛЬКИ {"is_retry": bool} і рахує ціну
    сама. Поле amount ігнорувалось, тож знижка на "Try again" не спрацьовувала
    жодного разу — списувався повний кредит замість 0.25 / 0.1.

    Ціну навмисно рахує функція, а не бекенд: вона ж перевіряє вікно ретраю
    (60 хв від останньої повної генерації) і лічильник retry_count_current < 3.
    PLAN_RETRY_COSTS нижче лишається довідковим — джерело істини у функції.
    """
    response = httpx.post(
        EDGE_FUNCTION_URL,
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json",
        },
        json={"is_retry": bool(is_retry), "dry_run": bool(dry_run)},
        timeout=15.0,
    )
    if response.status_code == 402:
        raise HTTPException(status_code=402, detail="Generation limit reached")
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to update generations")
    return response.json()


def charge_after_success(authorization: str, is_retry: bool = False):
    """
    Списання після того, як патерн готовий.

    Тут навмисно НЕ кидаємо помилку: патерн уже згенеровано і користувач має
    його отримати. Єдиний реалістичний шлях сюди з помилкою — гонка двох
    одночасних генерацій, яка коштує нам один зайвий запит до моделі, не більше.
    """
    try:
        return increment_generations(authorization, is_retry=is_retry, dry_run=False)
    except HTTPException as e:
        print(f"[charge] списання не пройшло після успішної генерації: {e.detail}")
    except Exception as e:
        print(f"[charge] списання не пройшло після успішної генерації: {e}")
    return None


def expand_symbols(symbols: list) -> list:
    KNOWN = {
        'sc', 'dc', 'hdc', 'tr', 'ch', 'sl', 'mr', 'inc', 'dec',
        'fpdc', 'bpdc', 'shell', 'bobble', 'cluster', 'picot', 'slst'
    }
    KNOWN_ORDERED = ['fpdc', 'bpdc', 'hdc', 'slst', 'shell', 'bobble', 'cluster', 'picot',
                     'sc', 'dc', 'tr', 'ch', 'sl', 'mr', 'inc', 'dec']

    def parse_one(raw):
        raw = str(raw).strip()
        lower = raw.lower().replace('sl st', 'slst').replace('slip stitch', 'slst')

        if 'magic ring' in lower:
            return ['mr']

        has_digits = bool(re.search(r'\d', lower))

        if not has_digits:
            result = []
            for k in KNOWN_ORDERED:
                if re.search(r'\b' + k + r'\b', lower):
                    result.append('sl' if k == 'slst' else k)
            return result if result else []

        found = []

        for m in re.finditer(r'(\d+)\s*[-]?\s*([a-z]+)', lower):
            stitch = m.group(2)
            if stitch == 'slst': stitch = 'sl'
            if stitch in KNOWN or stitch == 'sl':
                found.append((m.start(), int(m.group(1)), stitch))

        for m in re.finditer(r'([a-z]+)\s*[-]?\s*(\d+)', lower):
            stitch = m.group(1)
            if stitch == 'slst': stitch = 'sl'
            if stitch in KNOWN or stitch == 'sl':
                found.append((m.start(), int(m.group(2)), stitch))

        for m in re.finditer(
            r'\b(sc|dc|hdc|tr|ch|sl|inc|dec|fpdc|bpdc)\b([^,\[\]]{1,20}?)(\d+)\s*(?:st|sp|times|sts|x)?\b',
            lower
        ):
            stitch = m.group(1)
            if stitch == 'slst': stitch = 'sl'
            found.append((m.start(), int(m.group(3)), stitch))

        for k in KNOWN_ORDERED:
            for m in re.finditer(r'\b' + k + r'\b', lower):
                pos = m.start()
                before = lower[max(0, pos - 3):pos]
                after = lower[pos + len(k):pos + len(k) + 3]
                no_digit_right = not re.search(r'^\s*\d', after)
                no_digit_left_immediate = not re.search(r'\d\s*$', before)
                if no_digit_right and no_digit_left_immediate:
                    stitch = 'sl' if k == 'slst' else k
                    found.append((pos, 1, stitch))

        if found:
            seen_pos = set()
            unique = []
            for pos, count, stitch in sorted(found, key=lambda x: (-x[1], x[0])):
                if pos not in seen_pos:
                    seen_pos.add(pos)
                    unique.append((pos, count, stitch))
            unique.sort(key=lambda x: x[0])
            result = []
            for _, count, stitch in unique:
                result.extend([stitch] * min(count, 50))
            return result

        return []

    result = []
    for sym in symbols:
        result.extend(parse_one(sym))
    return result


def fix_chart_types(pattern: dict) -> dict:
    try:
        chart = pattern.get("chart")
        if not chart or not isinstance(chart, dict):
            return pattern
        sections = chart.get("sections")
        if not isinstance(sections, list):
            return pattern
        section_instructions = {}
        for sec in pattern.get("sections", []):
            if not isinstance(sec, dict):
                continue
            name = sec.get("name", "")
            rows = sec.get("rows", [])
            instructions = []
            for row in rows:
                if isinstance(row, dict):
                    instr = row.get("instruction", "")
                    if instr:
                        instructions.append(instr.lower())
                elif isinstance(row, str):
                    instructions.append(row.lower())
            section_instructions[name] = instructions
        for section in sections:
            if not isinstance(section, dict):
                continue
            name = section.get("name", "")
            rounds = section.get("rounds", [])
            for r in rounds:
                if isinstance(r, dict) and "symbols" in r:
                    r["symbols"] = expand_symbols(r["symbols"])
            has_magic_ring = False
            for r in rounds:
                if isinstance(r, dict):
                    notes = (r.get("notes", "") or "").lower()
                    if "magic ring" in notes:
                        has_magic_ring = True
                        break
            if has_magic_ring:
                section["type"] = "round"
                continue
            has_turn = False
            for instr in section_instructions.get(name, []):
                if "turn" in instr:
                    has_turn = True
                    break
            if not has_turn:
                for r in rounds:
                    if isinstance(r, dict):
                        notes = (r.get("notes", "") or "").lower()
                        if "turn" in notes:
                            has_turn = True
                            break
            if has_turn:
                section["type"] = "flat"
                continue
            section["type"] = "cylinder"
    except Exception:
        pass
    return pattern


def ensure_assembly(pattern: dict) -> dict:
    """
    Робить відсутню збірку видимою.

    Модель час від часу повертає патерн із кількох деталей і порожнім assembly —
    користувач отримує сім шматків і жодної підказки, як їх з'єднати. Мовчки це
    пропускати не можна: краще явна позначка в патерні, ніж PDF, у якому просто
    немає розділу.
    """
    try:
        sections = pattern.get("sections") or []
        assembly = pattern.get("assembly") or []
        assembly = [str(s).strip() for s in assembly if str(s).strip()]
        if len(sections) > 1 and not assembly:
            names = [s.get("name", "?") for s in sections if isinstance(s, dict)]
            pattern["assembly"] = [
                "Assembly steps were not generated for this pattern. "
                "Pieces to join: " + ", ".join(names) + ".",
                "Sew the pieces together with mattress stitch, stuff firmly "
                "before closing the final seam, and weave in all ends.",
            ]
        else:
            pattern["assembly"] = assembly
    except Exception:
        pass
    return pattern


# ─────────────────────────── автоматична перевірка ───────────────────────────

def _parse_gauge(gauge: str):
    """
    Витягує щільність: скільки петель і рядів на сантиметр.

    Модель віддає щільність у кількох різних формах, і жодну з них не гарантує.
    Комбінований формат ("14 sc x 16 rows = 10 cm x 10 cm") трапляється частіше
    за роздільний, і саме на ньому розбір спершу ламався — а без щільності не
    рахується готовий розмір, тобто не працює головна перевірка.

    Повертає (петель_на_см, рядів_на_см) або None.
    """
    if not gauge or not isinstance(gauge, str):
        return None
    text = gauge.lower().replace("\u00d7", "x").replace(",", ";")
    unit_re = r"(cm|centimet\w*|in|inch|inches|\")"

    def to_cm(size: float, unit: str) -> float:
        return size * 2.54 if unit.startswith(("in", '"')) else size

    # 1. Комбінований: "14 sc x 16 rows = 10 cm x 10 cm" або "... = 10 cm"
    m = re.search(
        r"([\d.]+)\s*(?:sc|sts?|stitches)\s*(?:x|by|and)\s*([\d.]+)\s*rows?"
        r"\s*=\s*([\d.]+)\s*" + unit_re +
        r"(?:\s*(?:x|by|and)\s*([\d.]+)\s*" + unit_re + r")?",
        text)
    if m:
        sts_count, rows_count = float(m.group(1)), float(m.group(2))
        width = to_cm(float(m.group(3)), m.group(4))
        height = to_cm(float(m.group(5)), m.group(6)) if m.group(5) else width
        if width > 0 and height > 0:
            return sts_count / width, rows_count / height

    # 2. Роздільний: "10 sc = 5 cm; 10 rows = 5 cm"
    def one(pattern):
        found = re.search(pattern, text)
        if not found:
            return None
        count, size = float(found.group(1)), float(found.group(2))
        size = to_cm(size, found.group(3))
        return count / size if size > 0 else None

    sts = one(r"([\d.]+)\s*(?:sc|sts?|stitches)\s*=\s*([\d.]+)\s*" + unit_re)
    rows = one(r"([\d.]+)\s*rows?\s*=\s*([\d.]+)\s*" + unit_re)
    if sts:
        return sts, (rows or sts)
    return None


def _rows_of(section: dict):
    """Ряди секції у вигляді (номер, кількість_петель, текст)."""
    out = []
    for row in section.get("rows") or []:
        if not isinstance(row, dict):
            continue
        count = row.get("stitch_count")
        if isinstance(count, (int, float)):
            out.append((row.get("row_number"), float(count), row.get("instruction") or ""))
    return out


def _check_chart_arithmetic(pattern: dict, issues: list):
    """
    Кількість петель наступного ряду має випливати з попереднього.

    Джерело істини — chart, бо там є increases/decreases списками позицій.
    prev + len(increases) - len(decreases) має дорівнювати поточному значенню.
    """
    chart = pattern.get("chart")
    if not isinstance(chart, dict):
        return
    for section in chart.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = section.get("name", "?")
        prev = None
        for rnd in section.get("rounds") or []:
            if not isinstance(rnd, dict):
                continue
            count = rnd.get("stitch_count")
            if not isinstance(count, (int, float)):
                continue
            count = float(count)
            inc = len(rnd.get("increases") or [])
            dec = len(rnd.get("decreases") or [])
            if prev is not None and (inc or dec):
                expected = prev + inc - dec
                if abs(expected - count) > 0.01:
                    issues.append({
                        "section": name,
                        "row": rnd.get("round"),
                        "kind": "count",
                        "text": (f"stitch count {count:g} does not follow from the "
                                 f"previous round: {prev:g} + {inc} inc - {dec} dec "
                                 f"= {expected:g}"),
                    })
            prev = count


def _check_text_matches_count(pattern: dict, issues: list):
    """
    Число в дужках наприкінці інструкції має збігатися зі stitch_count.

    Це те, що бачить користувач: якщо в тексті "(30)", а в даних 32, то
    діаграма і текст розійшлися, і хтось із них бреше.
    """
    for section in pattern.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = section.get("name", "?")
        for number, count, text in _rows_of(section):
            m = re.search(r"\((\d+)(?:\s*sc)?\)\s*$", text.strip())
            if not m:
                continue
            stated = float(m.group(1))
            if abs(stated - count) > 0.01:
                issues.append({
                    "section": name,
                    "row": number,
                    "kind": "count",
                    "text": (f"the instruction says ({stated:g}) but the pattern data "
                             f"says {count:g} stitches"),
                })


def _compute_size(pattern: dict, gauge_pair):
    """
    Рахує готовий розмір зі щільності. Значення моделі не перевіряємо, а
    замінюємо: вона стабільно подає обхват як діаметр, і це найчастіша
    помилка з усіх (перевірено на кількох патернах).

    Кругла деталь: найбільший ряд — це обхват, отже діаметр = обхват / пі.
    Пласка деталь: найбільший ряд — це ширина.
    """
    sts_per_cm, rows_per_cm = gauge_pair
    chart_types = {}
    chart = pattern.get("chart")
    if isinstance(chart, dict):
        for section in chart.get("sections") or []:
            if isinstance(section, dict):
                chart_types[section.get("name")] = (section.get("type") or "").lower()

    best = None
    for section in pattern.get("sections") or []:
        if not isinstance(section, dict):
            continue
        rows = _rows_of(section)
        if not rows:
            continue
        name = section.get("name", "?")
        max_sts = max(r[1] for r in rows)
        kind = chart_types.get(name, "flat")
        if kind in ("round", "cylinder", "cone"):
            across = (max_sts / sts_per_cm) / math.pi
        else:
            across = max_sts / sts_per_cm
        height = len(rows) / rows_per_cm
        largest = max(across, height)
        if best is None or largest > best[0]:
            best = (largest, name, across, height, kind)

    if not best:
        return None
    _, name, across, height, kind = best
    shape = "diameter" if kind in ("round", "cylinder", "cone") else "wide"
    return (f"~{across:.1f} cm {shape}, ~{height:.1f} cm tall "
            f"(largest piece: {name}; calculated from gauge)",
            across, height)


def _check_assembly_covers_sections(pattern: dict, issues: list):
    """Кожна деталь, крім першої, має згадуватись у кроках збірки."""
    sections = [s.get("name") for s in (pattern.get("sections") or [])
                if isinstance(s, dict) and s.get("name")]
    if len(sections) < 2:
        return
    assembly_text = " ".join(str(s) for s in (pattern.get("assembly") or [])).lower()
    if not assembly_text:
        return
    missing = [n for n in sections[1:] if n.lower() not in assembly_text]
    if missing:
        issues.append({
            "section": None,
            "row": None,
            "kind": "assembly",
            "text": "assembly steps do not mention: " + ", ".join(missing),
        })


def _annotate_rows(pattern: dict, issues: list):
    """
    Дописує попередження в текст самого ряду.

    Свідомо не додаємо нове поле в структуру: інструкція вже рендериться і на
    сторінці патерна, і в PDF, тож позначка з'явиться скрізь одразу, без
    правок на фронтенді.
    """
    by_key = {}
    for issue in issues:
        if issue["kind"] != "count" or issue["row"] is None:
            continue
        by_key.setdefault((issue["section"], issue["row"]), []).append(issue["text"])

    for section in pattern.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = section.get("name", "?")
        for row in section.get("rows") or []:
            if not isinstance(row, dict):
                continue
            notes = by_key.get((name, row.get("row_number")))
            if notes and row.get("instruction"):
                row["instruction"] = (
                    f"{row['instruction']}  [!] Check this row: {notes[0]}"
                )


def _mark_repeated_rows(pattern: dict):
    """
    Позначає ідентичні сусідні ряди, щоб фронтенд міг згорнути їх у діапазон.

    Ribblr пише "4-5 Sc around (18)" одним рядком замість двох однакових —
    патерн стає помітно коротшим і читабельнішим. Саме згортання лишається за
    фронтендом (там чекбокси на кожен ряд), бекенд лише проставляє розмітку:
      repeat_group — спільний номер для групи однакових рядів
      repeat_of    — номер першого ряду групи (None у самого першого)

    Порівнюємо за кількістю петель і текстом інструкції без номера ряду.
    """
    def normalize(text: str) -> str:
        text = re.sub(r"^\s*(?:row|rnd|round)\s*\d+[.:]?\s*", "", text or "",
                      flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", text).strip().lower()

    for section in pattern.get("sections") or []:
        if not isinstance(section, dict):
            continue
        rows = [r for r in (section.get("rows") or []) if isinstance(r, dict)]
        group = 0
        prev_key = None
        first_number = None
        for row in rows:
            key = (row.get("stitch_count"), normalize(row.get("instruction", "")))
            if prev_key is not None and key == prev_key:
                row["repeat_group"] = group
                row["repeat_of"] = first_number
            else:
                group += 1
                first_number = row.get("row_number")
                row["repeat_group"] = group
                row["repeat_of"] = None
            prev_key = key


def _check_chart_matches_sections(pattern: dict, issues: list):
    """
    Звіряє діаграму з текстом: це два описи однієї деталі.

    Знайдено на живому патерні: Leaf у тексті "(7 sts along chain)", у діаграмі
    "R1 St 14". Кожен опис окремо виглядав несуперечливим, тому попередні
    перевірки цього не бачили.

    Хто саме правий — не вирішуємо: це вимагало б розуміння техніки. Наша справа
    показати, що два джерела розходяться, і назвати обидва числа.
    """
    chart = pattern.get("chart")
    if not isinstance(chart, dict):
        return

    text_rows = {}
    for section in pattern.get("sections") or []:
        if isinstance(section, dict) and section.get("name"):
            key = str(section["name"]).strip().lower()
            text_rows[key] = {n: c for n, c, _ in _rows_of(section) if n is not None}

    for section in chart.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = section.get("name", "?")
        rows = text_rows.get(str(name).strip().lower())
        if not rows:
            continue

        rounds = [r for r in (section.get("rounds") or []) if isinstance(r, dict)]

        for rnd in rounds:
            number = rnd.get("round")
            count = rnd.get("stitch_count")
            if number is None or not isinstance(count, (int, float)):
                continue
            in_text = rows.get(number)
            if in_text is None:
                continue
            if abs(float(count) - in_text) > 0.01:
                issues.append({
                    "section": name,
                    "row": number,
                    "kind": "count",
                    "text": (f"the written instructions say {in_text:g} stitches "
                             f"but the chart says {float(count):g}"),
                })

        if rounds and rows and len(rounds) != len(rows):
            issues.append({
                "section": name,
                "row": None,
                "kind": "structure",
                "text": (f"the written instructions have {len(rows)} rows but the "
                         f"chart has {len(rounds)} — one of them is missing a row"),
            })


def _check_prose_dimensions(pattern: dict, computed, issues: list):
    """
    Шукає розмірні твердження в прозі й звіряє їх з обчисленим.

    Замінити finished_size недостатньо: модель дублює розмір у кроках збірки
    ("the finished pumpkin should stand approximately 20 cm tall"), і саме це
    число читає людина. На гарбузі різниця була втричі.

    Беремо лише фрази, де число прямо описує виріб — tall / wide / across /
    high / long / in diameter. Довжини хвостиків пряжі, розміри гачка й голки
    сюди не потрапляють, бо коло них таких слів немає.
    """
    if not computed:
        return

    max_cm = max(computed)
    if max_cm <= 0:
        return

    dimension_re = re.compile(
        r"([\d.]+)\s*(cm|centimet\w*|in|inch|inches|\")\s*"
        r"(tall|high|wide|across|long|in\s+diameter|in\s+width|in\s+height)",
        re.IGNORECASE)

    def scan(text, where):
        if not isinstance(text, str):
            return
        for match in dimension_re.finditer(text):
            value = float(match.group(1))
            if match.group(2).lower().startswith(("in", '"')):
                value *= 2.54
            # Цікавлять лише грубі розбіжності: у прозі повно легітимних
            # довжин, а обчислення теж має похибку через щільність.
            if value > max_cm * 1.4 or value < max_cm * 0.6:
                issues.append({
                    "section": where,
                    "row": None,
                    "kind": "size",
                    "text": (f"the text claims {match.group(0).strip()}, but the "
                             f"gauge and stitch counts give about "
                             f"{computed[0]:.1f} cm across and {computed[1]:.1f} cm tall"),
                })
                return  # одного зауваження на фрагмент досить

    for index, step in enumerate(pattern.get("assembly") or [], start=1):
        scan(step, f"Assembly step {index}")

    for section in pattern.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for row in section.get("rows") or []:
            if isinstance(row, dict):
                scan(row.get("instruction"), section.get("name"))
                scan(row.get("notes"), section.get("name"))

    scan(pattern.get("finished_size_stated_by_model"), "stated finished size")


def validate_pattern(pattern: dict) -> dict:
    """
    Перевіряє патерн арифметикою і позначає знайдене.

    Помилки НЕ виправляються і патерн НЕ перегенеровується: це подвоїло б час
    генерації, від якого ми й так потерпаємо. Замість цього користувач бачить,
    де саме варто перерахувати. Виняток — готовий розмір: його ми рахуємо самі
    й підставляємо, бо модель помиляється тут стабільно.
    """
    try:
        issues = []
        _check_chart_arithmetic(pattern, issues)
        _check_text_matches_count(pattern, issues)
        _check_assembly_covers_sections(pattern, issues)
        _check_chart_matches_sections(pattern, issues)

        gauge_pair = _parse_gauge(pattern.get("gauge", ""))
        if gauge_pair:
            result = _compute_size(pattern, gauge_pair)
            if result:
                text, across, height = result
                stated = pattern.get("finished_size")
                pattern["finished_size"] = text
                pattern["finished_size_stated_by_model"] = stated
                # Замінити поле недостатньо: модель дублює розмір прозою
                # в кроках збірки, і читають саме її.
                _check_prose_dimensions(pattern, (across, height), issues)

        _annotate_rows(pattern, issues)
        _mark_repeated_rows(pattern)

        checks = sum(len(s.get("rows") or []) for s in (pattern.get("sections") or []))
        pattern["validation"] = {
            "checked": True,
            "checks_run": checks,
            "issues_found": len(issues),
            "issues": issues[:20],
            "size_calculated": bool(gauge_pair),
            "summary": (f"Checked {checks} rows automatically — no discrepancies found"
                        if not issues else
                        f"Checked {checks} rows automatically — {len(issues)} to review"),
        }
    except Exception as exc:
        pattern["validation"] = {"checked": False, "error": str(exc)[:200]}
    return pattern


def sanitize_svg(svg: str):
    try:
        svg = svg.replace("\n", "").replace("\r", "").replace("\t", "").strip()
        if not svg.startswith("<svg") or not svg.endswith("</svg>"):
            return None
        return svg
    except Exception:
        return None


SYSTEM_PROMPT = """You are an expert crochet pattern designer. When given a description, generate a complete, accurate crochet pattern in strict JSON format.

CRITICAL RULES:
- ASSEMBLY IS MANDATORY. If the pattern has more than one section, the
  "assembly" array MUST be filled with concrete steps saying WHICH piece
  goes WHERE, with position and orientation, plus stuffing and closing.
  A pattern without assembly steps is unusable and counts as invalid output.
- Stitch counts MUST be mathematically correct. Double-check every increase/decrease round.
- Use standard US crochet terminology and abbreviations.
- Every row/round must have a stitch count in parentheses.
- Include gauge, materials, and finished measurements.
- For yarn, always specify a REAL yarn brand and name (e.g. "Drops Safran", "Lion Brand Pound of Love", "Paintbox Simply DK"). Match yarn weight to the item difficulty and size. Never just write "yarn" or a weight category like "worsted".
- Yarn yardage estimates must be realistic for the item and yarn weight.
- Patterns must be suitable for the stated difficulty level.
- Choose realistic yarn colors that match the item description.
  If user mentions a color, use it as Main color.
  If item needs only one color, still return colors array with one item.
  Always return hex codes for chosen colors.
- ALWAYS generate ALL parts of the item. For amigurumi include body, 
  head, all limbs, ears, tail, fins, and any other details as separate sections.
  Never generate only the main body and skip other parts.
  Each separate piece that needs to be crocheted independently must have 
  its own section in both sections and chart.sections.
  Examples:
  - Whale: Body, Tail, Dorsal Fin, Pectoral Fins (x2)
  - Teddy bear: Body, Head, Arms (x2), Legs (x2), Ears (x2)
  - Mushroom: Cap, Stem, optional Spots
  - Hat: Brim, Body, Crown
- Use ALL standard crochet stitches when appropriate:
  sc (single crochet), dc (double crochet), hdc (half double crochet),
  tr (treble crochet), sl st (slip stitch), ch (chain),
  inc (increase = 2sc in same st), dec (decrease = sc2tog),
  fpdc (front post dc), bpdc (back post dc),
  bobble, shell, cluster, picot

SIZE AND SHAPE RULES:
- The requested size is a REQUIREMENT, not a hint. If the user asks for ~20 cm,
  the finished piece must measure approximately 20 cm at its largest dimension.
  Compute the stitch counts from the stated gauge so that the result actually
  reaches that size, and make finished_size reflect the real computed value.
- Before writing rounds, check: at the given gauge, does the final stitch count
  produce the requested size? If not, adjust the number of increase rounds.
- Reproduce the SHAPE the user describes. A house-shaped pillow means a
  house silhouette, not a circle with a house appliqued on it. Only fall back to
  a simple geometric base when the description genuinely calls for one.

ROW NUMBERING:
- Number rows WITHIN each section, starting from 1 in every section.
  Do NOT continue numbering across sections. Section "Body" rows 1..12,
  then section "Stem" rows 1..3 — not 13..15.

INLINE NOTES:
- Put practical notes on the ROW where they are needed, in that row's "notes"
  field — not only at the end of the pattern. Examples: "Insert safety eyes
  between rounds 5 and 6, 4 stitches apart", "Start stuffing now", "Change to
  the second color", "Fasten off and leave a long tail".
  A maker follows the pattern top to bottom; a note that arrives after the piece
  is closed is useless.
- Keep assembly for joining separate pieces together. Anything that happens
  WHILE working a piece belongs in that piece's rows.

ASSEMBLY RULES:
- assembly is mandatory whenever the pattern has more than one section, and it
  must be specific enough to act on. Each step names WHICH piece goes WHERE,
  with position and orientation.
  GOOD: "Center the Roof triangle on the upper third of the Front Face, aligning
         its base with round 8, and sew with mattress stitch."
  BAD:  "Sew the roof onto the front."
- Cover every section that is not the base piece. If a piece is made twice
  (two ears, two windows), say where each one goes.
- Mention stuffing and closing as separate steps where relevant.

CHART RULES:
- Each round object contains ONLY these three keys: stitch_count, symbols, notes.
  Do NOT write "round", "shape_change", "color_name", "increases" or "decreases" —
  the server derives all of them from symbols and stitch_count. Writing them
  wastes output and is ignored.
- notes: any special instruction for that round (magic ring, fasten off, stuff before closing etc).
  Leave notes as an empty string when there is nothing special.
- symbols array: use COMPACT run-length notation. Write "<count> <stitch>" for
  consecutive identical stitches instead of repeating them one by one. The
  server expands this automatically, so a round of 26 single crochets is
  ["26 sc"], not twenty-six separate entries.
  Allowed stitch codes: sc, dc, hdc, tr, ch, sl, inc, dec, fpdc, bpdc, mr
  CORRECT: ["mr", "6 sc"]
  CORRECT: ["dec", "22 sc", "dec"]
  CORRECT: ["3 sc", "inc", "3 sc", "inc"]
  WRONG:   ["sc","sc","sc","sc","sc","sc", ...twenty more...]
  WRONG:   ["2 dc in ring", "ch-2 sp"]   (prose, not a stitch code)
  Keep the total stitch count implied by symbols equal to stitch_count.
- For chart type follow this STRICT decision tree — check in this exact order:

  STEP 1: Does round 1 notes contain "magic ring"?
          YES → type = "round". STOP. Do not check anything else.
          
  STEP 2: Does any row instruction contain "turn"?
          YES → type = "flat". STOP.
          
  STEP 3: Everything else → type = "cylinder".

  CRITICAL: "magic ring" in notes ALWAYS means "round", even if the piece is called
  "body", "head", "tail", or anything else. Never override this with "cylinder".

RESPOND WITH ONLY VALID JSON — no markdown, no explanation, no code fences.

JSON structure:
{
  "title": "Pattern name",
  "difficulty": "Easy|Medium|Hard",
  "finished_size": "dimensions",
  "gauge": "X sc = X inches",
  "materials": {
    "yarn_weight": "specific yarn name and weight, e.g. 'Paintbox Simply DK' or 'Lion Brand Pound of Love Worsted'",
    "yarn_yardage": 100,
    "hook_size": "size",
    "extras": ["item1"]
  },
  "colors": [
    {
      "name": "Main color",
      "hex": "#hexcolor",
      "description": "primary yarn color for the main body"
    }
  ],
  "svg_type": "beanie|sweater|scarf|amigurumi|bag|blanket|socks|mittens|toy",
  "sections": [
    {
      "name": "Section name",
      "color_name": "Main color",
      "rows": [
        {
          "id": "row_1",
          "row_number": 1,
          "instruction": "full instruction here",
          "stitch_count": 6
        }
      ]
    }
  ],
  "chart": {
    "sections": [
      {
        "name": "Section name",
        "type": "round|cylinder|flat|cone|triangle|square",
        "color_name": "Main color",
        "shape_change": "expanding|decreasing|straight",
        "rounds": [
          {
            "stitch_count": 6,
            "symbols": ["mr", "6 sc"],
            "notes": "magic ring start"
          }
        ]
      }
    ]
  },
  "assembly": ["step1", "step2"]
}"""

SVG_SYSTEM_PROMPT = """You are an SVG illustration artist specializing in cute crochet toy illustrations.
Generate a simple cute SVG illustration of a finished crochet item.

STRICT RULES:
- viewBox must be exactly "0 0 200 400"
- Use ONLY basic shapes: ellipse, circle, rect, path
- Max 20 SVG elements total
- Style: cute, round, soft, like a children's toy illustration
- Include subtle stitch texture pattern in defs
- Add cute face (eyes + smile) if item is animal or toy
- The illustration MUST be clearly recognizable as the specific item
- Use double quotes for ALL attributes
- Return single line SVG with no line breaks
- NO text elements

RESPOND WITH ONLY THE RAW SVG — nothing else, no markdown, no explanation."""


ALLOWED_DIFFICULTIES = {"Beginner", "Easy", "Intermediate", "Advanced"}
ALLOWED_UNITS = {"cm", "inches"}


class GenerateRequest(BaseModel):
    idea: str = Field(..., min_length=3, max_length=500)
    difficulty: str = "Easy"
    size: str = Field(default="Standard", max_length=100)
    units: str = "cm"
    user_id: str = Field(...)
    is_retry: bool = Field(default=False)

    @field_validator("idea")
    @classmethod
    def strip_idea(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("idea must be at least 3 characters")
        return v

    @field_validator("difficulty")
    @classmethod
    def check_difficulty(cls, v: str) -> str:
        if v not in ALLOWED_DIFFICULTIES:
            raise ValueError(f"difficulty must be one of {sorted(ALLOWED_DIFFICULTIES)}")
        return v

    @field_validator("units")
    @classmethod
    def check_units(cls, v: str) -> str:
        if v not in ALLOWED_UNITS:
            raise ValueError(f"units must be one of {sorted(ALLOWED_UNITS)}")
        return v


class SvgRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    colors: list

    @field_validator("colors")
    @classmethod
    def check_colors(cls, v: list) -> list:
        if len(v) > 10:
            raise ValueError("colors must contain at most 10 items")
        return v


@app.get("/")
def root():
    return {"status": "StitchMagic API is running", "model": "claude-haiku-4-5-20251001"}


@app.post("/api/generate")
def generate_pattern(
    request_body: GenerateRequest,
    authorization: Optional[str] = Header(default=None)
):
    # Токен з Authorization header
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization missing")
    auth_header = authorization

    # Rate limiting по токену юзера
    check_rate_limit(auth_header)

    # Читаємо план для вибору моделі і розрахунку вартості
    profile = get_user_profile(auth_header)
    plan = profile.get("plan", "free")
    model = PLAN_MODELS.get(plan, "claude-haiku-4-5-20251001")

    # Вартість рахує Edge Function increment-generations: вона єдина бачить
    # last_full_generation_at і retry_count_current, тобто може перевірити, що
    # ретрай справді йде за реальною генерацією, а не є способом отримати
    # знижку на кожну. Прапорець від клієнта сам по собі знижки не дає.
    # Перевірка без запису: якщо стежок не вистачає — 402 ще до моделі.
    # Саме списання йде нижче, після того як патерн реально готовий.
    increment_generations(auth_header, is_retry=request_body.is_retry, dry_run=True)

    try:
        message = _stream_message(
            _label="generate",
            model=model,
            # Кейси 3.12 і 3.16: при 8192 довгі патерни і неанглійські запити
            # обривались на середині JSON, і користувач отримував 500 після
            # того, як кредит уже списано. Кирилиця коштує втричі більше
            # токенів на той самий текст, тому впиралась у стелю першою.
            max_tokens=32000,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Design a crochet pattern.\nIdea: {request_body.idea}\nDifficulty: {request_body.difficulty}\nREQUIRED FINISHED SIZE: {request_body.size} — the finished piece must actually measure this. Derive stitch counts from the gauge to reach it.\nIMPORTANT: Use {request_body.units} for ALL measurements. Gauge must be in {request_body.units}. Finished size must be in {request_body.units}. Do not use any other unit of measurement.\n\nEvery separate piece must be listed in assembly with its exact position on the main piece. Return ONLY the JSON object."
                }
            ]
        )

        # Обрив по стелі токенів діагностуємо ЯВНО. Раніше він проявлявся як
        # "Invalid JSON from Claude" десь на 24-тисячному символі, і причина
        # була неочевидна ні в логах, ні користувачу.
        if getattr(message, "stop_reason", None) == "max_tokens":
            raise HTTPException(
                status_code=422,
                detail="Pattern too large to generate. Try a simpler idea or a smaller size.",
            )

        text = _strip_code_fences(message.content[0].text)
        pattern = json.loads(text)
        pattern = fix_chart_types(pattern)
        pattern = enrich_chart(pattern)
        pattern = ensure_assembly(pattern)

        charge_after_success(auth_header, is_retry=request_body.is_retry)
        pattern = validate_pattern(pattern)
        return {"pattern": pattern}

    except json.JSONDecodeError:
        # Кредит уже списано до виклику моделі, тож користувач за цю генерацію
        # заплатив. Даємо ще одну спробу за наш рахунок замість того, щоб
        # повертати помилку на оплачений запит.
        try:
            retry_message = _stream_message(
                _label="generate-retry",
                model=model,
                max_tokens=32000,
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": f"Design a crochet pattern.\nIdea: {request_body.idea}\nDifficulty: {request_body.difficulty}\nSize / scale: {request_body.size}\nIMPORTANT: Use {request_body.units} for ALL measurements. Keep the pattern compact: no more than 8 sections. Return ONLY the JSON object, nothing else."
                    }
                ],
            )
            if getattr(retry_message, "stop_reason", None) == "max_tokens":
                raise HTTPException(
                    status_code=422,
                    detail="Pattern too large to generate. Try a simpler idea or a smaller size.",
                )
            pattern = json.loads(_strip_code_fences(retry_message.content[0].text))
            pattern = fix_chart_types(pattern)
            pattern = enrich_chart(pattern)
            pattern = ensure_assembly(pattern)
            charge_after_success(auth_header, is_retry=request_body.is_retry)
            pattern = validate_pattern(pattern)
            return {"pattern": pattern}
        except HTTPException:
            raise
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=502,
                detail=f"Could not build a valid pattern this time. Please try again. ({e.msg})",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/generate-svg")
def generate_svg(request: SvgRequest):
    try:
        colors_str = ", ".join([f"{c.get('name')} ({c.get('hex')})" for c in request.colors])
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            system=SVG_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Generate SVG illustration for: {request.title}\nColors: {colors_str}\n\nReturn ONLY the SVG."
                }
            ]
        )

        svg_raw = message.content[0].text.strip()
        svg = sanitize_svg(svg_raw)

        if not svg:
            raise HTTPException(status_code=500, detail="Invalid SVG generated")

        return {"svg": svg}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/founder-slots")
def founder_slots():
    anon_client: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    result = anon_client.rpc("get_founder_slots_remaining").execute()
    slots = result.data if result.data is not None else 100
    return {"slots_remaining": slots}


@app.get("/health")
def health():
    return {"status": "ok"}
