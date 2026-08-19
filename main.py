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


SIZING_PROMPT = """You are a crochet materials expert. Given an item description and a
target finished size, choose the yarn and hook, and state the gauge they produce.

Answer with ONLY a JSON object, no prose, no code fences:
{"yarn": "real brand and product name with weight",
 "hook_size": "e.g. 5mm",
 "sts_per_10cm": 14,
 "rows_per_10cm": 16}

Use a REAL yarn brand and product. Pick a weight that suits the item and the
requested size: bigger items take heavier yarn. sts_per_10cm and rows_per_10cm
must be realistic for that yarn worked in single crochet."""


# Обхват грудей у сантиметрах за міжнародними літерними розмірами.
# Беремо середину діапазону: XS 81-86, S 86-91, M 91-96 і так далі.
GARMENT_BUST_CM = {
    "xxs": 78, "xs": 83, "s": 88, "m": 93, "l": 101,
    "xl": 111, "2xl": 121, "xxl": 121, "3xl": 131, "xxxl": 131, "4xl": 141,
}

# Припуск: наскільки виріб більший за тіло. Без нього светр просто не налізе.
GARMENT_EASE_CM = 10


def _garment_size(size_text):
    """
    Розпізнає літерний розмір одягу і повертає потрібний обхват у сантиметрах.

    Селектор шле рядки виду "M (bust 91-96 cm)". Якщо в тексті є явні числа
    обхвату — беремо їх, вони точніші за таблицю. Інакше — за літерою.

    Повертає (обхват_з_припуском, підпис) або None, якщо це не одяг.
    """
    text = str(size_text or "").lower()

    explicit = re.search(r"bust\s*(\d+)\s*[-–]\s*(\d+)\s*cm", text)
    if explicit:
        bust = (float(explicit.group(1)) + float(explicit.group(2))) / 2
        letter = re.match(r"\s*(xxs|xs|s|m|l|xl|2xl|xxl|3xl|xxxl|4xl)\b", text)
        label = letter.group(1).upper() if letter else f"{bust:.0f} cm bust"
        return bust + GARMENT_EASE_CM, label

    letter = re.match(r"\s*(xxs|xs|s|m|l|xl|2xl|xxl|3xl|xxxl|4xl)\b", text)
    if letter:
        key = letter.group(1)
        if key in GARMENT_BUST_CM:
            return GARMENT_BUST_CM[key] + GARMENT_EASE_CM, key.upper()
    return None


def _target_stitches(size_text, sts_per_cm):
    """
    Скільки петель у найширшому ряду дасть замовлений розмір.

    Дві різні формули, і плутати їх — головне джерело помилок:

    Іграшка в круговій в'язці: найширший ряд це обхват, тож для діаметра D
    треба D * пі * щільність. Модель тут стабільно множила без пі і давала
    виріб утричі менший.

    Одяг: літерний розмір це вже обхват грудей, пі не потрібне — але потрібен
    припуск, інакше річ не налізе.

    Повертає (петлі, розмір_см, підпис) або None, якщо розмір не розібрано.
    """
    if not sts_per_cm or sts_per_cm <= 0:
        return None

    def rounded(circumference):
        return max(int(round(circumference * sts_per_cm / 6.0)) * 6, 6)

    garment = _garment_size(size_text)
    if garment:
        circumference, label = garment
        return rounded(circumference), circumference, f"{label} garment (chest circumference)"

    match = re.search(r"([\d.]+)\s*(cm|centimet\w*|in|inch|inches|\")",
                      str(size_text or ""), re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    if match.group(2).lower().startswith(("in", '"')):
        value *= 2.54
    if value <= 0 or value > 300:
        return None
    return rounded(value * math.pi), value, f"{value:.0f} cm across"


def _plan_materials(model, idea, size_text, units):
    """
    Перший, короткий виклик: пряжа, гачок, щільність.

    Свідомо без SYSTEM_PROMPT: там кілька екранів правил про діаграми і збірку,
    які тут не потрібні, а обробка їх з'їла б увесь виграш у часі. Відповідь —
    чотири поля, тобто секунди.
    """
    try:
        message = _stream_message(
            model=model,
            max_tokens=300,
            system=SIZING_PROMPT,
            messages=[{"role": "user",
                       "content": f"Item: {idea}\nTarget finished size: {size_text}\n"
                                  f"Units: {units}\nAnswer with the JSON object only."}],
        )
        data = json.loads(_strip_code_fences(message.content[0].text))
        sts = float(data.get("sts_per_10cm") or 0)
        rows = float(data.get("rows_per_10cm") or 0)
        if sts <= 0:
            return None
        return {
            "yarn": str(data.get("yarn") or "").strip(),
            "hook_size": str(data.get("hook_size") or "").strip(),
            "sts_per_cm": sts / 10.0,
            "rows_per_cm": (rows or sts) / 10.0,
            "gauge_text": f"{sts:g} sc = 10 cm, {(rows or sts):g} rows = 10 cm",
        }
    except Exception:
        # Не вдалось — генеруємо як раніше. Розмір тоді перевірить валідатор.
        return None


def _sizing_brief(plan_data, target):
    """Текст із готовими числами, який додається до основного запиту."""
    if not plan_data or not target:
        return ""
    stitches, size_cm, label = target
    is_garment = "garment" in label
    where = ("widest round of the body" if is_garment
             else "widest round of the main piece")
    explain = (f"That is a chest circumference of {size_cm:.0f} cm including ease, "
               f"which is what size {label.split()[0]} needs."
               if is_garment else
               f"At this gauge that gives a finished width of about {size_cm:.0f} cm, "
               f"which is what the user asked for.")
    return (
        f"\nMATERIALS AND SIZE ARE ALREADY DECIDED — use exactly these:\n"
        f"- Yarn: {plan_data['yarn']}\n"
        f"- Hook: {plan_data['hook_size']}\n"
        f"- Gauge: {plan_data['gauge_text']}\n"
        f"- Target: {label}\n"
        f"- The {where} MUST have {stitches} stitches. {explain} "
        f"Build up to exactly {stitches} stitches — do not stop earlier.\n"
        f"- Put this gauge string in the gauge field verbatim: {plan_data['gauge_text']}\n"
    )


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


CHART_STITCH = r"(?:sc2tog|dc2tog|hdc2tog|fpdc|bpdc|sl\s*st|slst|hdc|sc|dc|tr|inc|dec|ch)"

CHART_CANON = {
    "sc2tog": "dec", "dc2tog": "dec", "hdc2tog": "dec",
    "slst": "sl", "slst ": "sl", "sl": "sl",
}


def _symbols_of_block(text: str) -> list:
    """Перелік стібків у порядку виконання: "sc 3, inc" -> [sc, sc, sc, inc]."""
    out = []
    for m in re.finditer(rf"(?:(\d+)\s*)?\b({CHART_STITCH})\b(?:\s*(\d+))?", text):
        name = re.sub(r"\s+", "", m.group(2))
        name = CHART_CANON.get(name, name)
        if name == "ch":
            continue  # ланцюжок — основа, а не стібок ряду
        count = int(m.group(1) or m.group(3) or 1)
        if count > 400:
            return []
        out.extend([name] * count)
        if len(out) > 1000:
            return []
    return out


def _symbols_from_instruction(text, previous):
    """
    Символи діаграми з тексту інструкції. None — якщо ряд підрахунку не піддається.

    Ті самі межі, що й у лічильника петель: проза, відносні формулювання і
    в'язання вздовж обох боків ланцюжка чесно повертають None.
    """
    if not isinstance(text, str) or not text.strip():
        return None

    body = text.lower()
    body = re.sub(r"\(\s*\d+[^)]*\)\s*$", " ", body)
    body = re.sub(r"sl\s*st\b[^,.;]*\b(?:join|close|attach)\b", " ", body)

    if re.search(r"other side|both sides|around the (?:foundation )?chain|"
                 r"across until|until \d+\s*sts?\s*remain|to last \d*\s*sts?|"
                 r"across first \d+|remain(?:ing)? unworked|leave last|"
                 r"each side|for (?:back |front )?neck|fasten off center|divide for",
                 body):
        return None

    magic_ring = bool(re.search(r"magic ring|magic circle", body))
    symbols = None

    repeat = re.search(
        r"[\*\[](?P<block>[^*\]]+)[\*\]]\s*(?:rep(?:eat)?(?:\s+from\s*\*)?\s*)?"
        r"(?:around\s*)?(?:x\s*)?(?P<times>\d+)\s*(?:times)?", body)
    if repeat:
        block = _symbols_of_block(repeat.group("block"))
        times = int(repeat.group("times"))
        if block and 0 < times <= 200:
            symbols = block * times + _symbols_of_block(body[repeat.end():])

    if symbols is None and previous:
        if re.search(r"\binc\b[^.]{0,20}\b(?:in\s+)?each\b", body):
            symbols = ["inc"] * previous
        else:
            shrink = re.search(r"\b(?:dec|sc2tog)\b\s*(\d+)\s*times", body)
            plain = re.search(r"\b(sc|dc|hdc)\b[^.]{0,20}\b(?:in\s+)?each\s+st", body)
            if shrink:
                times = int(shrink.group(1))
                if 0 < times * 2 <= previous:
                    symbols = ["dec"] * times + ["sc"] * (previous - times * 2)
            elif plain:
                symbols = [plain.group(1)] * previous

    if symbols is None:
        listed = _symbols_of_block(body)
        symbols = listed if listed else None

    if symbols is None:
        return None
    if magic_ring:
        symbols = ["mr"] + symbols
    return symbols


def _expand_row_ranges(pattern: dict) -> dict:
    """
    Розгортає компактні діапазони рядів у окремі ряди.

    Модель пише {"row_number": 3, "row_number_end": 10, ...} одним об'єктом —
    це прибирає близько половини виводу. Тут діапазон повертається у звичайні
    ряди, тому решта конвеєра (build_chart, валідатор) і фронтенд працюють
    так само, як і до цієї зміни.

    Обережність навмисна: розгортаємо лише те, у чому впевнені.
      - кінець має бути більшим за початок і не далі ніж на 200 рядів;
      - stitch_count має бути один на весь діапазон (він і є ознакою того,
        що ряди однакові);
      - усе інше лишаємо як є і позначаємо, щоб валідатор побачив.
    """
    MAX_SPAN = 200
    for section in pattern.get("sections") or []:
        if not isinstance(section, dict):
            continue
        rows = section.get("rows")
        if not isinstance(rows, list):
            continue

        expanded = []
        for row in rows:
            if not isinstance(row, dict):
                continue

            start = row.get("row_number")
            end = row.pop("row_number_end", None)

            if end is None:
                expanded.append(row)
                continue

            try:
                start_i = int(start)
                end_i = int(end)
            except (TypeError, ValueError):
                expanded.append(row)
                continue

            span = end_i - start_i
            if span <= 0 or span > MAX_SPAN:
                # Діапазон безглуздий — беремо як один ряд, нічого не вигадуємо.
                expanded.append(row)
                continue

            count = row.get("stitch_count")
            if not isinstance(count, (int, float)) or count <= 0:
                # Без сталої кількості петель це не однакові ряди.
                # Розгортати наосліп означало б вигадати дані.
                row["range_not_expanded"] = True
                expanded.append(row)
                continue

            base_instruction = row.get("instruction") or ""
            base_notes = row.get("notes") or ""

            for offset in range(span + 1):
                number = start_i + offset
                clone = {
                    "id": "row_%d" % number,
                    "row_number": number,
                    "instruction": base_instruction,
                    "stitch_count": count,
                }
                if row.get("color_name"):
                    clone["color_name"] = row["color_name"]
                # Нотатка стосується місця, а не кожного ряду: лишаємо її
                # на останньому ряду діапазону ("рубчик закінчено"), інакше
                # вона повторилася б вісім разів поспіль.
                if base_notes and offset == span:
                    clone["notes"] = base_notes
                expanded.append(clone)

        section["rows"] = expanded
    return pattern


def build_chart(pattern: dict) -> dict:
    """
    Складає діаграму з тексту патерна.

    Модель більше не пише блок chart — це половина виводу і головне джерело
    розбіжностей між текстом і діаграмою. Тут та сама деталь описується один
    раз, тому розійтись вони не можуть.
    """
    try:
        existing = pattern.get("chart")
        if isinstance(existing, dict) and existing.get("sections"):
            return pattern  # модель прислала свою — не чіпаємо

        chart_sections = []
        for section in pattern.get("sections") or []:
            if not isinstance(section, dict):
                continue
            rounds = []
            previous = None
            for row in section.get("rows") or []:
                if not isinstance(row, dict):
                    continue
                instruction = row.get("instruction") or ""
                stated = row.get("stitch_count")
                stated = int(stated) if isinstance(stated, (int, float)) and stated > 0 else None

                symbols = _symbols_from_instruction(instruction, previous)
                if symbols is None:
                    # Ряд підрахунку не піддався: рівний ряд за заявленою
                    # кількістю. Краще рівний, ніж викривлений.
                    if not stated:
                        previous = None
                        continue
                    symbols = ["sc"] * min(stated, 400)

                count = stated
                if count is None:
                    count = sum(2 if s == "inc" else 0 if s in ("mr", "sl") else 1
                                for s in symbols)
                    count = count or None
                if count is None:
                    previous = None
                    continue

                rounds.append({
                    "stitch_count": count,
                    "symbols": symbols,
                    "notes": row.get("notes") or "",
                })
                previous = count

            if rounds:
                chart_sections.append({
                    "name": section.get("name") or "",
                    "type": "cylinder",  # уточнить fix_chart_types()
                    "color_name": section.get("color_name"),
                    "rounds": rounds,
                })

        if chart_sections:
            pattern["chart"] = {"sections": chart_sections}
    except Exception:
        pass
    return pattern


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


def _core_name(name) -> str:
    """
    Назва деталі без уточнень: "Leaf (make 2)" -> "leaf".

    Модель додає до назв дужки з кількістю чи методом, а в кроках збірки пише
    коротко ("First Leaf", "Pumpkin Ribbed Body"). Порівняння повних назв давало
    хибні спрацювання.
    """
    text = re.sub(r"\([^)]*\)", " ", str(name or ""))
    text = re.sub(r"\b(?:make|x)\s*\d+\b", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip().lower()


def _check_assembly_covers_sections(pattern: dict, issues: list):
    """
    Кожна деталь, крім першої, має згадуватись у кроках збірки.

    Порівнюємо за назвою без дужок і за окремими словами: у збірці деталь може
    згадуватись як "First Leaf" там, де секція зветься "Leaf (make 2)".
    """
    sections = [s.get("name") for s in (pattern.get("sections") or [])
                if isinstance(s, dict) and s.get("name")]
    if len(sections) < 2:
        return
    assembly_text = " ".join(str(s) for s in (pattern.get("assembly") or [])).lower()
    if not assembly_text:
        return

    missing = []
    for name in sections[1:]:
        core = _core_name(name)
        if not core:
            continue
        words = [w for w in core.split() if len(w) > 2]
        if core in assembly_text:
            continue
        if words and all(w in assembly_text for w in words):
            continue
        missing.append(name)

    if missing:
        issues.append({
            "section": None,
            "row": None,
            "kind": "assembly",
            "text": "assembly steps do not mention: " + ", ".join(missing),
        })


def _check_duplicate_sections(pattern: dict, issues: list):
    """
    Ловить альтернативні версії однієї деталі.

    Модель інколи дає два корпуси одразу — основний і "Alternative Single-Piece
    Method". У збірці згадується лише один, другий лишається мертвим вантажем:
    людина витратить кілька годин на деталь, яка нікуди не йде.
    """
    sections = [s.get("name") for s in (pattern.get("sections") or [])
                if isinstance(s, dict) and s.get("name")]
    flagged = [n for n in sections
               if re.search(r"\b(alternative|option|version|variant|method b|instead of)\b",
                            str(n), re.IGNORECASE)]
    if flagged:
        issues.append({
            "section": None,
            "row": None,
            "kind": "structure",
            "text": ("the pattern offers alternative versions of the same piece ("
                     + ", ".join(flagged) + ") — only one should be given"),
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





def _count_from_instruction(text, previous):
    """
    Рахує петлі з тексту інструкції — незалежно від заявлених чисел.

    Потрібне, щоб при розбіжності між текстом і діаграмою можна було сказати,
    хто з них правий, а не лише що вони не сходяться.

    Повертає число або None, якщо ряд описаний прозою. Ланцюжок (ch) до петель
    не рахується: це основа, а не стібки ряду.
    """
    if not isinstance(text, str) or not text.strip():
        return None

    body = text.lower()
    body = re.sub(r"\(\s*\d+[^)]*\)\s*$", " ", body)   # прибрати заявлене "(30)"
    # Прибрати фразу приєднання: "sl st to first sc to join" — це не нові петлі,
    # а вказівка, куди приєднатись. Згадане там "sc" рахувалось як стібок і
    # давало 8 замість 6 у першому ряду з магічним кільцем.
    body = re.sub(r"sl\s*st\b[^,.;]*\b(?:join|close|attach)\b", " ", body)

    # В'язання вздовж обох боків ланцюжка ("work 11 sts along the other
    # side") простим додаванням не рахується: частина петель описана
    # словами, а не переліком стібків. Краще промовчати, ніж дати число,
    # якого в ряду немає.
    if re.search(r"other side|both sides|around the (?:foundation )?chain", body):
        return None

    # Відносні вказівки: "sc across until 2 sts remain", "work to last st",
    # "sl st across first 4 sts". Скільки саме петель вони означають, з тексту
    # не видно — це виводиться з попереднього ряду, а не з переліку стібків.
    # Мова саме одягу; для амігурумі кожен ряд самодостатній.
    if re.search(r"across until|until \d+\s*sts?\s*remain|to last \d*\s*sts?|"
                 r"across first \d+|remain(?:ing)? unworked|leave last", body):
        return None

    # Розділення роботи на частини: кількість петель падає законно.
    if re.search(r"each side|for (?:back |front )?neck|shoulder(?:s)? |"
                 r"fasten off center|divide for", body):
        return None

    STITCH = r"(?:sc2tog|dc2tog|hdc2tog|fpdc|bpdc|slst|sl\s*st|hdc|sc|dc|tr|inc|dec)"

    def value_of(name, count):
        clean = re.sub(r"\s+", "", name)
        if clean == "inc":
            return count * 2          # приріст: дві петлі з однієї
        return count                  # спад дає одну; решта — як є

    # 1. Повтор блоку: "*sc 3, inc* repeat 6 times" / "*sc 3, inc* x6"
    # Модель пише повтор і зірочками, і квадратними дужками:
    #   "*sc 3, inc* repeat 6 times"   і   "[Sc 3, dec] repeat 6 times"
    # Розпізнавались лише зірочки, тому ряди в дужках рахувались як окремі
    # стібки: "[sc 3, dec] repeat 4 times, sc 2" давало 6 замість 18.
    repeat = re.search(
        r"[\*\[](?P<block>[^*\]]+)[\*\]]\s*(?:rep(?:eat)?(?:\s+from\s*\*)?\s*)?"
        r"(?:around\s*)?(?:x\s*)?(?P<times>\d+)\s*(?:times)?", body)
    if repeat:
        per_block = 0
        found = False
        for m in re.finditer(rf"(?:(\d+)\s*)?\b({STITCH})\b(?:\s*(\d+))?",
                             repeat.group("block")):
            per_block += value_of(m.group(2), int(m.group(1) or m.group(3) or 1))
            found = True
        if found and per_block > 0:
            total = per_block * int(repeat.group("times"))
            # Хвіст після повтору теж рахується: "…repeat 4 times, sc 2"
            tail = body[repeat.end():]
            for m in re.finditer(rf"(?:(\d+)\s*)?\b({STITCH})\b(?:\s*(\d+))?", tail):
                total += value_of(m.group(2), int(m.group(1) or m.group(3) or 1))
            return total

    # 2. Проста дія на весь ряд — рахується від попереднього
    if previous:
        if re.search(r"\binc\b[^.]{0,20}\b(?:in\s+)?each\b", body):
            return previous * 2
        shrink = re.search(r"\b(?:dec|sc2tog)\b\s*(\d+)\s*times", body)
        if shrink:
            return previous - int(shrink.group(1))
        if re.search(r"\b(?:sc|dc|hdc)\b[^.]{0,20}\b(?:in\s+)?each\s+st", body):
            return previous

    # 3. Перелік стібків підряд
    listed = re.findall(rf"(?:(\d+)\s*)?\b({STITCH})\b(?:\s*(\d+))?", body)
    if len(listed) >= 3:
        total = sum(value_of(name, int(a or b or 1)) for a, name, b in listed)
        if total > 0:
            return total

    return None


def _fix_prose_dimensions(pattern, computed):
    """
    Замінює хибні розмірні числа в прозі на обчислені.

    Виправляємо лише розмір, бо тут є одна правильна відповідь: вона виводиться
    зі щільності й кількості петель, а не з думки моделі. Модель помиляється
    стабільно — зазвичай подає обхват як діаметр, тобто втричі більше.

    Числа в межах 40% від обчисленого лишаються недоторканими: там і наш
    розрахунок має похибку, і формулювання "приблизно" виправдане.

    Повертає кількість виправлених місць.
    """
    if not computed:
        return 0
    across, height = computed
    if across <= 0 or height <= 0:
        return 0

    dimension_re = re.compile(
        r"([\d.]+)\s*(cm|centimet\w*|in|inch|inches|\")\s*"
        r"(tall|high|wide|across|long|in\s+diameter|in\s+width|in\s+height)",
        re.IGNORECASE)

    state = {"fixed": 0}

    def swap(match):
        value = float(match.group(1))
        unit = match.group(2).lower()
        what = match.group(3).lower()
        if unit.startswith(("in", '"')) and not what.startswith("in "):
            value *= 2.54
        correct = height if re.search(r"tall|high|height", what) else across
        if abs(value - correct) <= max(correct * 0.4, 0.5):
            return match.group(0)
        state["fixed"] += 1
        return f"{correct:.1f} cm {match.group(3)}"

    def replace_in(text):
        return dimension_re.sub(swap, text) if isinstance(text, str) and text else text

    if isinstance(pattern.get("assembly"), list):
        pattern["assembly"] = [replace_in(s) for s in pattern["assembly"]]

    for section in pattern.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for row in section.get("rows") or []:
            if not isinstance(row, dict):
                continue
            for field in ("instruction", "notes"):
                if isinstance(row.get(field), str):
                    row[field] = replace_in(row[field])

    return state["fixed"]


def _adjudicate_counts(pattern, issues):
    """
    Там, де текст і діаграма розійшлись, каже, хто правий.

    Якщо підрахунок не збігається з жодним — теж повідомляє: три різні числа це
    саме те, що людині треба знати, щоб перерахувати ряд самій.
    """
    chart_counts = {}
    chart = pattern.get("chart")
    if isinstance(chart, dict):
        for section in chart.get("sections") or []:
            if not isinstance(section, dict):
                continue
            key = str(section.get("name", "")).strip().lower()
            for rnd in section.get("rounds") or []:
                if isinstance(rnd, dict) and isinstance(rnd.get("stitch_count"), (int, float)):
                    chart_counts[(key, rnd.get("round"))] = float(rnd["stitch_count"])

    for section in pattern.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = section.get("name", "?")
        key = str(name).strip().lower()
        previous = None
        for row in section.get("rows") or []:
            if not isinstance(row, dict):
                continue
            number = row.get("row_number")
            stated = row.get("stitch_count")
            derived = _count_from_instruction(row.get("instruction", ""), previous)
            if isinstance(stated, (int, float)):
                previous = float(stated)
            if derived is None or not isinstance(stated, (int, float)):
                continue

            stated = float(stated)
            in_chart = chart_counts.get((key, number))
            matches_text = abs(derived - stated) < 0.01
            matches_chart = in_chart is not None and abs(derived - in_chart) < 0.01

            if in_chart is not None and abs(stated - in_chart) > 0.01:
                if matches_text or matches_chart:
                    winner = "written instructions" if matches_text else "chart"
                    issues.append({
                        "section": name, "row": number, "kind": "count",
                        "text": (f"counting the stitches in this row gives {derived:g}, "
                                 f"which matches the {winner} — the other source is wrong"),
                    })
                else:
                    issues.append({
                        "section": name, "row": number, "kind": "count",
                        "text": (f"counting the stitches written in this row gives "
                                 f"{derived:g}, which matches neither the instructions "
                                 f"({stated:g}) nor the chart ({in_chart:g})"),
                    })
                continue

            if not matches_text:
                issues.append({
                    "section": name, "row": number, "kind": "count",
                    "text": (f"the row says {stated:g} stitches, but counting the "
                             f"stitches written in it gives {derived:g}"),
                })


def _pattern_text(pattern):
    """Увесь текст патерна одним рядком — інструкції, нотатки, збірка."""
    parts = []
    for section in pattern.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for row in section.get("rows") or []:
            if isinstance(row, dict):
                parts.append(str(row.get("instruction") or ""))
                parts.append(str(row.get("notes") or ""))
    for step in pattern.get("assembly") or []:
        parts.append(str(step))
    return " ".join(parts).lower()


# Що згадується в інструкціях і має бути в матеріалах. Ключ — як шукаємо в
# тексті, значення — як називаємо користувачу.
REQUIRED_SUPPLIES = {
    r"safety eyes?": "safety eyes",
    r"fiberfill|polyfill|stuffing|stuff (?:the|it|firmly)": "stuffing (fiberfill)",
    r"stitch marker": "stitch marker",
    r"yarn needle|tapestry needle|darning needle": "yarn needle",
    r"pipe cleaner": "pipe cleaner",
    r"floral wire|craft wire": "wire",
    r"embroidery floss": "embroidery floss",
    r"felt\b": "felt",
    r"hot glue|fabric glue": "glue",
    r"button": "buttons",
    r"ribbon": "ribbon",
}


def _check_materials(pattern, issues):
    """
    Звіряє матеріали, згадані в інструкціях, зі списком матеріалів.

    Найпрактичніша з усіх перевірок: без очей чи наповнювача роботу не почати,
    а дізнатись про це посеред в'язання — найгірший момент.
    """
    materials = pattern.get("materials")
    listed = ""
    if isinstance(materials, dict):
        listed = " ".join(str(v) for v in materials.values() if v is not None).lower()
        extras = materials.get("extras")
        if isinstance(extras, list):
            listed += " " + " ".join(str(e) for e in extras).lower()

    text = _pattern_text(pattern)
    missing = []
    for probe, label in REQUIRED_SUPPLIES.items():
        if re.search(probe, text) and not re.search(probe, listed):
            missing.append(label)

    if missing:
        issues.append({
            "section": None, "row": None, "kind": "materials",
            "text": ("the instructions use supplies that are not in the materials "
                     "list: " + ", ".join(sorted(set(missing)))),
        })


def _check_yarn_quantity(pattern, issues):
    """
    Кількість пряжі має бути вказана — інакше її неможливо купити.

    Перевіряємо і числове поле, і згадку в тексті матеріалів: модель інколи
    пише кількість словами замість заповнити поле.
    """
    materials = pattern.get("materials")
    if not isinstance(materials, dict):
        return
    yardage = materials.get("yarn_yardage")
    if isinstance(yardage, (int, float)) and yardage > 0:
        return
    described = str(materials.get("yarn_weight") or "").lower()
    if re.search(r"\d+\s*(?:m|metre|meter|yd|yard|g|gram|oz|ball|skein)", described):
        return
    issues.append({
        "section": None, "row": None, "kind": "materials",
        "text": "no yarn quantity given — the maker cannot know how much to buy",
    })


def _check_spiral_vs_joined(pattern, issues):
    """
    Амігурумі в'яжеться спіраллю, не з'єднаними рядами.

    "Ch 1 ... sl st to join" щоряду — це техніка для шапки: на іграшці лишається
    видимий шов збоку. Плюс не сказано, куди йде перша петля наступного ряду —
    у петлю з'єднання чи в наступну, і саме ця двозначність збиває підрахунки.
    """
    chart = pattern.get("chart")
    round_sections = set()
    if isinstance(chart, dict):
        for section in chart.get("sections") or []:
            if isinstance(section, dict) and str(section.get("type", "")).lower() in (
                    "round", "cylinder"):
                round_sections.add(str(section.get("name", "")).strip().lower())

    if not round_sections:
        return

    for section in pattern.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = section.get("name", "?")
        if str(name).strip().lower() not in round_sections:
            continue
        rows = [r for r in (section.get("rows") or []) if isinstance(r, dict)]
        if len(rows) < 4:
            continue
        joined = sum(1 for r in rows
                     if re.search(r"sl\s*st[^,.;]*\bjoin\b",
                                  str(r.get("instruction") or ""), re.IGNORECASE))
        if joined >= len(rows) * 0.6:
            issues.append({
                "section": name, "row": None, "kind": "technique",
                "text": ("this piece is worked in the round but joins every round "
                         "with a slip stitch — that leaves a visible seam. Amigurumi "
                         "is normally worked in a continuous spiral with a stitch marker"),
            })


def _group_issues(issues):
    """
    Згортає однакові зауваження в одне.

    Рукав светра дав дев'ять окремих рядків про той самий зсув діаграми на один
    ряд. Дев'ять однакових повідомлень читаються як дев'ять різних проблем і
    ховають справжні знахідки.
    """
    if len(issues) < 3:
        return issues

    buckets = {}
    order = []
    for issue in issues:
        shape = re.sub(r"\d+", "#", str(issue.get("text", "")))
        key = (issue.get("section"), issue.get("kind"), shape)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(issue)

    grouped = []
    for key in order:
        same = buckets[key]
        if len(same) < 3:
            grouped.extend(same)
            continue
        rows = [i.get("row") for i in same if isinstance(i.get("row"), int)]
        where = f"rows {min(rows)}-{max(rows)}" if rows else "several rows"
        grouped.append({
            "section": same[0].get("section"),
            "row": None,
            "kind": same[0].get("kind"),
            "text": f"{same[0]['text']} (and {len(same) - 1} more like it, {where})",
        })
    return grouped


# ---------------------------------------------------------------------------
# Craft Yarn Council standards (static tables, no network at runtime).
# Body measurements: https://www.craftyarncouncil.com/standards/body-sizing
# Yarn weight system: https://www.craftyarncouncil.com/standards/yarn-weight-system
# ---------------------------------------------------------------------------

# weight number -> (names, hook mm range, sc per 10 cm range, typical use)
CYC_YARN_WEIGHTS = {
    0: (("lace", "thread", "cobweb", "10-count", "fingering 0"), (1.6, 2.25), (32, 42),
        "lace shawls and doilies"),
    1: (("super fine", "superfine", "fingering", "sock", "baby 1", "4-ply"), (2.25, 3.5), (21, 32),
        "socks, shawls, baby items"),
    2: (("fine", "sport", "baby"), (3.5, 4.5), (16, 20), "light garments and baby wear"),
    3: (("dk", "light worsted", "light", "double knit", "double knitting", "8-ply"), (4.5, 5.5), (12, 17),
        "garments, amigurumi, accessories"),
    4: (("medium", "worsted", "aran", "afghan", "10-ply"), (5.5, 6.5), (11, 14),
        "sweaters, blankets, hats"),
    5: (("bulky", "chunky", "craft", "rug", "12-ply"), (6.5, 9.0), (8, 11),
        "fast blankets and outerwear"),
    6: (("super bulky", "superbulky", "roving", "16-ply"), (9.0, 15.0), (6, 9),
        "chunky blankets and scarves"),
    7: (("jumbo", "roving jumbo"), (15.0, 25.0), (0, 6), "arm knitting and giant blankets"),
}

# Amigurumi is worked far tighter than the label suggests, so the usual hook
# range does not apply - the fabric has to hide the stuffing.
CYC_AMIGURUMI_OFFSET = 1.0

# label -> (chest cm, back length cm, sleeve length cm)
CYC_SIZES_WOMEN = {
    "Women XS": (81, 37, 41), "Women S": (89, 38, 42), "Women M": (97, 39, 43),
    "Women L": (107, 40, 43), "Women 1X": (117, 41, 44), "Women 2X": (127, 42, 44),
    "Women 3X": (137, 43, 45),
}
CYC_SIZES_MEN = {
    "Men S": (89, 46, 84), "Men M": (99, 48, 86), "Men L": (109, 50, 87),
    "Men XL": (119, 52, 89), "Men 2X": (129, 53, 90), "Men 3X": (139, 55, 92),
}
CYC_SIZES_CHILD = {
    "Child 2": (53, 23, 27), "Child 4": (58, 26, 30), "Child 6": (65, 29, 34),
    "Child 8": (70, 32, 37), "Child 10": (75, 34, 40), "Child 12": (80, 37, 43),
    "Child 14": (85, 39, 45),
}

# label -> head circumference cm
CYC_HEADS = {
    "Baby": 36, "Toddler": 46, "Child": 50, "Teen": 54, "Adult S": 55,
    "Adult M": 57, "Adult L": 59,
}

# label -> foot length cm
CYC_FEET = {
    "Child": 18, "Women S": 22, "Women M": 24, "Women L": 25,
    "Men M": 26, "Men L": 28,
}

_GARMENT_WORDS = (
    ("sweater", ("sweater", "jumper", "pullover", "hoodie", "top-down")),
    ("cardigan", ("cardigan", "shrug", "bolero")),
    ("hat", ("hat", "beanie", "bonnet", "cap", "slouch")),
    ("socks", ("sock", "slipper", "bootie")),
    ("mittens", ("mitten", "glove")),
    ("scarf", ("scarf", "cowl", "snood")),
    ("blanket", ("blanket", "afghan", "throw", "rug")),
    ("amigurumi", ("amigurumi", "plush", "toy", "doll", "bear", "bunny", "cat",
                   "dog", "octopus", "dino", "pumpkin", "monster", "penguin")),
    ("bag", ("bag", "basket", "purse", "tote", "pouch")),
)

_WEARABLES = ("sweater", "cardigan", "hat", "socks", "mittens")


def detect_garment_type(pattern: dict) -> str:
    """Rough item class from title, notes and section names."""
    parts = [str(pattern.get("title") or ""), str(pattern.get("notes") or "")]
    for section in pattern.get("sections") or []:
        if isinstance(section, dict):
            parts.append(str(section.get("name") or ""))
    text = " ".join(parts).lower()
    for kind, words in _GARMENT_WORDS:
        if any(w in text for w in words):
            return kind
    return "unknown"


def _is_child(pattern: dict) -> bool:
    text = (str(pattern.get("title") or "") + " " + str(pattern.get("notes") or "")).lower()
    return any(w in text for w in ("child", "kid", "baby", "toddler", "girl", "boy", "infant"))


def _cyc_section_dims(pattern: dict, gauge_pair):
    """
    Per-section measurements in cm: {name: {"circ", "across", "length", "kind"}}.

    Round and cylinder pieces measure as a circumference; flat pieces as a width.
    """
    sts_per_cm, rows_per_cm = gauge_pair
    if not sts_per_cm or not rows_per_cm:
        return {}

    kinds = {}
    chart = pattern.get("chart")
    if isinstance(chart, dict):
        for section in chart.get("sections") or []:
            if isinstance(section, dict):
                kinds[section.get("name")] = (section.get("type") or "").lower()

    dims = {}
    for section in pattern.get("sections") or []:
        if not isinstance(section, dict):
            continue
        rows = _rows_of(section)
        if not rows:
            continue
        name = str(section.get("name") or "?")
        max_sts = max(r[1] for r in rows)
        kind = kinds.get(section.get("name"), "flat")
        width = max_sts / sts_per_cm
        dims[name.lower()] = {
            "name": name,
            "kind": kind,
            "circ": width,
            "across": width / math.pi if kind in ("round", "cylinder", "cone") else width,
            "length": len(rows) / rows_per_cm,
        }
    return dims


def _pick_section(dims: dict, *words):
    """Largest section whose name mentions one of the words."""
    hits = [d for key, d in dims.items() if any(w in key for w in words)]
    if not hits:
        return None
    return max(hits, key=lambda d: d["circ"])


def _nearest_size(table: dict, value: float, index=0):
    """Closest table entry to value; returns (label, tuple_or_number, delta)."""
    best = None
    for label, spec in table.items():
        target = spec[index] if isinstance(spec, tuple) else spec
        delta = value - target
        if best is None or abs(delta) < abs(best[2]):
            best = (label, spec, delta)
    return best


def _cyc_note(issues, text, section=None):
    issues.append({"section": section, "row": None, "kind": "proportion", "text": text})


def check_proportions(pattern: dict, gauge_pair, issues: list):
    """
    Compares the piece against CYC body measurements and records the size it
    matches. Toys, blankets, bags and anything unrecognised are skipped: there
    is no standard body to compare them to.

    Returns the size_match dict (or None) so the caller can store it.
    """
    kind = detect_garment_type(pattern)
    if kind not in _WEARABLES or not gauge_pair:
        return None

    dims = _cyc_section_dims(pattern, gauge_pair)
    if not dims:
        return None

    match = {"type": kind, "source": "CYC"}

    if kind in ("sweater", "cardigan"):
        body = (_pick_section(dims, "body", "torso", "front", "back", "yoke")
                or max(dims.values(), key=lambda d: d["circ"]))
        chest = body["circ"] if body["kind"] in ("round", "cylinder", "cone") else body["circ"] * 2
        table = CYC_SIZES_CHILD if _is_child(pattern) else CYC_SIZES_WOMEN
        label, spec, delta = _nearest_size(table, chest, 0)
        match.update({"size_label": label, "chest_cm": round(chest, 1)})
        if abs(delta) > max(6.0, spec[0] * 0.08):
            _cyc_note(issues,
                      f"chest measures about {chest:.0f} cm from the gauge - that sits between "
                      f"standard sizes (closest is {label}, {spec[0]} cm). Check the stitch count "
                      f"of the body if you are making a standard size.",
                      body["name"])
        sleeve = _pick_section(dims, "sleeve", "arm")
        if sleeve:
            want = spec[2]
            got = sleeve["length"]
            if abs(got - want) > max(5.0, want * 0.15):
                shorter = "shorter" if got < want else "longer"
                _cyc_note(issues,
                          f"the sleeve works out about {got:.0f} cm, which is {abs(got - want):.0f} cm "
                          f"{shorter} than the usual {want} cm for {label}.",
                          sleeve["name"])
            match["sleeve_cm"] = round(got, 1)
        length = body["length"]
        if length and abs(length - spec[1]) > max(6.0, spec[1] * 0.2):
            shorter = "shorter" if length < spec[1] else "longer"
            _cyc_note(issues,
                      f"the body works out about {length:.0f} cm long, {shorter} than the usual "
                      f"{spec[1]} cm for {label}.",
                      body["name"])
        match["length_cm"] = round(length, 1)

    elif kind == "hat":
        hat = (_pick_section(dims, "hat", "crown", "brim", "band", "body")
               or max(dims.values(), key=lambda d: d["circ"]))
        circ = hat["circ"]
        label, head, delta = _nearest_size(CYC_HEADS, circ + 3.0)
        match.update({"size_label": label, "head_cm": round(circ, 1)})
        # A hat has to stretch onto the head: 2-5 cm of negative ease.
        if circ >= head:
            _cyc_note(issues,
                      f"the hat measures about {circ:.0f} cm around, the same as or wider than the "
                      f"standard {label} head ({head} cm). Hats are usually worked 2-5 cm smaller so "
                      f"they stay on.",
                      hat["name"])

    elif kind == "socks":
        foot = _pick_section(dims, "foot", "toe", "sole") or max(dims.values(), key=lambda d: d["length"])
        label, want, delta = _nearest_size(CYC_FEET, foot["length"])
        match.update({"size_label": label, "foot_cm": round(foot["length"], 1)})
        if abs(delta) > 3.0:
            _cyc_note(issues,
                      f"the foot works out about {foot['length']:.0f} cm, {abs(delta):.0f} cm off the "
                      f"standard {want} cm for {label}.",
                      foot["name"])

    elif kind == "mittens":
        hand = max(dims.values(), key=lambda d: d["circ"])
        match.update({"size_label": "Child" if _is_child(pattern) else "Adult",
                      "hand_cm": round(hand["circ"], 1)})

    return match if match.get("size_label") else None


def _cyc_weight_of(text: str):
    """CYC weight number from a yarn description, or None."""
    if not text:
        return None
    low = str(text).lower()
    m = re.search(r"(?:weight|category|cyc)\s*#?\s*([0-7])\b", low)
    if m:
        return int(m.group(1))
    best = None
    for number, (names, _hook, _gauge, _use) in CYC_YARN_WEIGHTS.items():
        for name in names:
            if name in low and (best is None or len(name) > best[1]):
                best = (number, len(name))
    return best[0] if best else None


def _cyc_hook_mm(text: str):
    if not text:
        return None
    m = re.search(r"([\d.]+)\s*mm", str(text).lower())
    if m:
        try:
            value = float(m.group(1))
        except ValueError:
            return None
        return value if 1.0 <= value <= 30.0 else None
    return None


def check_yarn_fit(pattern: dict, gauge_pair, issues: list):
    """
    Yarn weight vs hook vs gauge vs what is being made. Every note is advisory:
    a designer may choose a tighter or looser fabric on purpose, and we say so
    rather than calling it an error.
    """
    materials = pattern.get("materials") if isinstance(pattern.get("materials"), dict) else {}
    yarn_text = " ".join(str(v) for v in (
        materials.get("yarn_weight"), materials.get("yarn"), pattern.get("yarn")) if v)
    hook_text = " ".join(str(v) for v in (
        materials.get("hook_size"), pattern.get("hook")) if v)

    weight = _cyc_weight_of(yarn_text)
    hook = _cyc_hook_mm(hook_text)
    kind = detect_garment_type(pattern)

    def note(text):
        issues.append({"section": None, "row": None, "kind": "yarn_fit", "text": text})

    if weight is None:
        if yarn_text.strip():
            note("the yarn weight is not stated in a standard way (lace, DK, worsted, "
                 "bulky...), so substituting yarn will be guesswork.")
        return None

    names, (hook_min, hook_max), (gauge_min, gauge_max), typical = CYC_YARN_WEIGHTS[weight]
    label = names[0].upper() if len(names[0]) <= 3 else names[0].title()

    if hook is not None:
        # Half a millimetre of slack: crocheters routinely go one hook either
        # way and the CYC window is a recommendation, not a rule.
        low, high = hook_min - 0.5, hook_max + 0.5
        if kind == "amigurumi":
            # Toys are worked tight on purpose - shift the whole window down.
            low, high = hook_min - CYC_AMIGURUMI_OFFSET, hook_max - 0.5
            if hook > high:
                note(f"{label} (CYC weight {weight}) worked at {hook:g} mm will leave gaps that "
                     f"show the stuffing; toys are usually made on {low:g}-{high:g} mm.")
        elif hook > high:
            note(f"{label} (CYC weight {weight}) is usually worked with a {hook_min:g}-{hook_max:g} mm "
                 f"hook; this pattern uses {hook:g} mm, so the fabric will be very open.")
        elif hook < low:
            note(f"{label} (CYC weight {weight}) is usually worked with a {hook_min:g}-{hook_max:g} mm "
                 f"hook; this pattern uses {hook:g} mm, so the fabric will be stiff and slow to work.")

    if gauge_pair:
        sts_per_10 = gauge_pair[0] * 10.0
        if gauge_max and sts_per_10 > gauge_max * 1.35:
            note(f"the gauge of {sts_per_10:.0f} sc per 10 cm is tighter than usual for {label} "
                 f"({gauge_min}-{gauge_max} sc). Fine for toys, hard work for a garment.")
        elif gauge_min and sts_per_10 < gauge_min * 0.7:
            note(f"the gauge of {sts_per_10:.0f} sc per 10 cm is looser than usual for {label} "
                 f"({gauge_min}-{gauge_max} sc) - check the hook size.")

    if kind in ("sweater", "cardigan") and weight >= 6:
        note(f"{label} yarn makes a very heavy garment; weight 3-4 is the usual choice for "
             f"a wearable {kind}.")
    if kind == "amigurumi" and weight >= 5:
        note(f"{label} yarn makes a large, loose toy; weight 3-4 holds the shape better.")
    if kind == "blanket" and weight <= 2:
        note(f"{label} yarn on a blanket means a very long make - weight 4-6 is the usual choice.")

    return {"weight": weight, "label": label, "hook_mm": hook, "typical_use": typical}


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
        _check_materials(pattern, issues)
        _check_yarn_quantity(pattern, issues)
        _check_spiral_vs_joined(pattern, issues)
        _check_duplicate_sections(pattern, issues)
        _check_chart_matches_sections(pattern, issues)
        _adjudicate_counts(pattern, issues)

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
                # Розмір ВИПРАВЛЯЄМО, а не лише позначаємо: тут є одна
                # правильна відповідь, обчислена зі щільності. Модель стабільно
                # подає обхват як діаметр — на гарбузі це давало 20 см замість 6.7.
                fixed = _fix_prose_dimensions(pattern, (across, height))
                if fixed:
                    pattern.setdefault("validation_fixes", []).append(
                        f"corrected {fixed} size claim(s) in the text to match the "
                        f"gauge: ~{across:.1f} cm across, ~{height:.1f} cm tall")
                _check_prose_dimensions(pattern, (across, height), issues)

        gauge_pair_cyc = _parse_gauge(pattern.get("gauge", ""))
        cyc_match = None
        try:
            cyc_match = check_proportions(pattern, gauge_pair_cyc, issues)
            check_yarn_fit(pattern, gauge_pair_cyc, issues)
        except Exception:
            cyc_match = None

        _annotate_rows(pattern, issues)
        _mark_repeated_rows(pattern)

        issues = _group_issues(issues)
        checks = sum(len(s.get("rows") or []) for s in (pattern.get("sections") or []))
        pattern["validation"] = {
            "checked": True,
            "checks_run": checks,
            "issues_found": len(issues),
            "issues": issues[:20],
            "size_calculated": bool(gauge_pair),
            "size_match": cyc_match,
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
- WORK AMIGURUMI IN A CONTINUOUS SPIRAL, not in joined rounds. Do not write
  "ch 1 ... sl st to join" on every round of a stuffed toy piece: that is hat
  construction and leaves a visible seam up the side. Say "place a stitch marker
  in the first stitch and move it up each round" instead. Joined rounds are
  correct only when the piece genuinely needs a defined edge, such as a hat brim.
- LIST EVERY SUPPLY the instructions rely on in materials.extras: safety eyes,
  stuffing, stitch marker, yarn needle, wire, felt, glue. If a round says
  "insert safety eyes", the eyes must appear in the materials list with a size.
- ALWAYS fill yarn_yardage with a realistic number. Without it the maker cannot
  buy yarn.
- ONE METHOD PER PIECE. Never give alternative versions of the same piece —
  no "Alternative Single-Piece Method", no "Option B", no "or you can instead".
  Choose the best approach and write only that one. Every entry in "sections"
  must be a separate physical piece the maker actually crochets, and every one
  of them must appear in the assembly steps.
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
  its own section.
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
- COLLAPSE IDENTICAL CONSECUTIVE ROWS into ONE object using "row_number_end".
  When several rows in a row have the SAME instruction and the SAME stitch
  count, write them once:
    {"row_number": 3, "row_number_end": 10,
     "instruction": "Ch 1, turn. BLO sc in each st across.",
     "stitch_count": 80}
  instead of eight near-identical objects. The server expands the range back
  into individual rows, so nothing is lost for the maker.
  Rules for collapsing:
    * ONLY when the instruction text and the stitch count are identical.
    * NEVER collapse rows that increase, decrease, change colour, or differ
      in any way — those must stay separate objects with their own counts.
    * Omit "row_number_end" entirely for a single row.
  This matters: a garment written row-by-row is several times longer than it
  needs to be.

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

CHART:
- Do NOT output a "chart" object. The server builds the chart from the written
  instructions, so anything you write there is discarded. Put every detail of a
  round into its "instruction" and "notes" instead.

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
          "row_number_end": 4,
          "instruction": "full instruction here",
          "stitch_count": 6
        }
      ]
    }
  ],
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

    # Спершу з'ясовуємо матеріали й щільність окремим коротким викликом, щоб
    # порахувати потрібну кількість петель. Інакше модель вирішує це сама і
    # стабільно помиляється: подає обхват як діаметр, і виріб виходить утричі
    # меншим за замовлений.
    sizing = _plan_materials(model, request_body.idea, request_body.size, request_body.units)
    target = _target_stitches(request_body.size, sizing["sts_per_cm"]) if sizing else None
    sizing_brief = _sizing_brief(sizing, target)

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
                    "content": f"Design a crochet pattern.\nIdea: {request_body.idea}\nDifficulty: {request_body.difficulty}\nREQUIRED FINISHED SIZE: {request_body.size} — the finished piece must actually measure this. Derive stitch counts from the gauge to reach it.\nIMPORTANT: Use {request_body.units} for ALL measurements. Gauge must be in {request_body.units}. Finished size must be in {request_body.units}. Do not use any other unit of measurement.\n\nEvery separate piece must be listed in assembly with its exact position on the main piece. Return ONLY the JSON object.{sizing_brief}"
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
        pattern = _expand_row_ranges(pattern)
        pattern = build_chart(pattern)
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
            pattern = _expand_row_ranges(pattern)
            pattern = build_chart(pattern)
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
