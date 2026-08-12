#!/usr/bin/env python3
"""
StitchMagic API — патч main.py за результатами QA (фази 2, 3, 14).

Виправляє:
  1. BUG-001 [P1] — битий JWT дає 500 замість 401 (кейс 2.4, 8+ повторень)
  2. BUG-008 [P2] — рейт-ліміт обходиться повторним логіном (кейс 14.3)
  3. BUG-004 [P1] — знижка на "Try again" ніколи не застосовується (кейси 3.4-3.6)
  4. Обрив відповіді моделі на довгих і неанглійських патернах (кейси 3.12, 3.16)
  5. Крихке зрізання ```-фенсів, здатне впасти з IndexError

Безпечний: робить резервну копію, перевіряє синтаксис після зміни,
при невдачі повертає оригінал.

Запуск із папки репозиторію stitchmagic-api:
    python patch_main.py
"""
import py_compile
import shutil
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parent / "main.py"


def fail(msg):
    print("\n  ПОМИЛКА:", msg, "\n")
    sys.exit(1)


def main():
    print("\n  Патч main.py за результатами QA\n  " + "=" * 33 + "\n")

    if not MAIN.exists():
        fail(f"не знайдено {MAIN}. Запусти з папки репозиторію stitchmagic-api.")

    src = MAIN.read_text(encoding="utf-8")

    if "_rate_limit_key" in src:
        print("  Файл уже пропатчений — нічого робити.\n")
        return 0

    backup = MAIN.with_suffix(".py.bak")
    shutil.copy2(MAIN, backup)
    print(f"  Резервна копія: {backup.name}")
    changes = 0

    # ── 1. Рейт-ліміт: ключ по user_id, а не по хвосту токена ───────────────
    old = '''def check_rate_limit(token: str):
    now = time.time()
    key = token[-16:]  # використовуємо останні 16 символів токена як ключ'''
    new = '''def _rate_limit_key(token: str) -> str:
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
    key = _rate_limit_key(token)'''
    if old not in src:
        shutil.move(str(backup), str(MAIN))
        fail("не знайдено check_rate_limit у звичному вигляді. Нічого не змінено.")
    src = src.replace(old, new, 1)
    changes += 1
    print("  + BUG-008: рейт-ліміт рахується по user_id, не по токену")

    # ── імпорти для нового коду ─────────────────────────────────────────────
    old = "import os\nimport json\nimport re\nimport time"
    new = "import os\nimport json\nimport re\nimport time\nimport base64\nimport hashlib"
    if old not in src:
        shutil.move(str(backup), str(MAIN))
        fail("не знайдено блок імпортів. Нічого не змінено.")
    src = src.replace(old, new, 1)
    changes += 1
    print("  + імпорти base64, hashlib")

    # ── 2. BUG-001: битий JWT → 401, не 500 ─────────────────────────────────
    old = '''    authed_client.postgrest.auth(token)
    user_resp = authed_client.auth.get_user(token)
    if not user_resp.user:
        raise HTTPException(status_code=401, detail="Unauthorized")'''
    new = '''    authed_client.postgrest.auth(token)
    # BUG-001 (кейс 2.4): на битому чи простроченому JWT get_user кидає виняток
    # бібліотеки, який без обробки перетворювався на 500. Правильна відповідь —
    # 401: клієнт надіслав невалідні дані, сервер справний. Усі Edge Functions
    # цього ж продукту обробляють той самий випадок саме так.
    try:
        user_resp = authed_client.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not user_resp or not user_resp.user:
        raise HTTPException(status_code=401, detail="Unauthorized")'''
    if old not in src:
        shutil.move(str(backup), str(MAIN))
        fail("не знайдено блок get_user. Нічого не змінено.")
    src = src.replace(old, new, 1)
    changes += 1
    print("  + BUG-001: битий JWT дає 401 замість 500")

    # Профіль теж може кинути виняток (немає рядка → .single() падає)
    old = '''    profile_resp = authed_client.table("profiles").select("*").eq("user_id", user_id).single().execute()
    if not profile_resp.data:
        raise HTTPException(status_code=404, detail="Profile not found")'''
    new = '''    try:
        profile_resp = authed_client.table("profiles").select("*").eq(
            "user_id", user_id).single().execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Profile not found")
    if not profile_resp or not profile_resp.data:
        raise HTTPException(status_code=404, detail="Profile not found")'''
    if old in src:
        src = src.replace(old, new, 1)
        changes += 1
        print("  + відсутній профіль дає 404 замість 500")

    # ── 3. BUG-004: знижка на Try again ─────────────────────────────────────
    old = '''def increment_generations(authorization: str, amount: float = 1.0):
    """Викликає Edge Function для безпечного списування генерацій"""
    response = httpx.post(
        EDGE_FUNCTION_URL,
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json",
        },
        json={"amount": amount},
        timeout=10.0,
    )'''
    new = '''def increment_generations(authorization: str, is_retry: bool = False):
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
        json={"is_retry": bool(is_retry)},
        timeout=15.0,
    )'''
    if old not in src:
        shutil.move(str(backup), str(MAIN))
        fail("не знайдено increment_generations. Нічого не змінено.")
    src = src.replace(old, new, 1)
    changes += 1
    print("  + BUG-004: у Edge Function передається is_retry замість amount")

    old = '''    # Вартість визначає ТІЛЬКИ бекенд: звичайна генерація = 1.0, Try again = за планом
    amount = PLAN_RETRY_COSTS.get(plan, 1.0) if request_body.is_retry else 1.0

    # Перевіряємо ліміт і списуємо генерацію через Edge Function
    increment_generations(auth_header, amount=amount)'''
    new = '''    # Вартість рахує Edge Function increment-generations: вона єдина бачить
    # last_full_generation_at і retry_count_current, тобто може перевірити, що
    # ретрай справді йде за реальною генерацією, а не є способом отримати
    # знижку на кожну. Прапорець від клієнта сам по собі знижки не дає.
    increment_generations(auth_header, is_retry=request_body.is_retry)'''
    if old not in src:
        shutil.move(str(backup), str(MAIN))
        fail("не знайдено виклик increment_generations у /api/generate.")
    src = src.replace(old, new, 1)
    changes += 1
    print("  + виклик у /api/generate оновлено")

    # ── 4. Обрив відповіді моделі ───────────────────────────────────────────
    old = '''        message = client.messages.create(
            model=model,
            max_tokens=8192,'''
    new = '''        message = client.messages.create(
            model=model,
            # Кейси 3.12 і 3.16: при 8192 довгі патерни і неанглійські запити
            # обривались на середині JSON, і користувач отримував 500 після
            # того, як кредит уже списано. Кирилиця коштує втричі більше
            # токенів на той самий текст, тому впиралась у стелю першою.
            max_tokens=16384,'''
    if old not in src:
        shutil.move(str(backup), str(MAIN))
        fail("не знайдено виклик messages.create з max_tokens=8192.")
    src = src.replace(old, new, 1)
    changes += 1
    print("  + max_tokens 8192 -> 16384")

    old = '''        text = message.content[0].text.strip()

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        pattern = json.loads(text)
        pattern = fix_chart_types(pattern)

        return {"pattern": pattern}

    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Invalid JSON from Claude: {str(e)}")'''
    new = '''        # Обрив по стелі токенів діагностуємо ЯВНО. Раніше він проявлявся як
        # "Invalid JSON from Claude" десь на 24-тисячному символі, і причина
        # була неочевидна ні в логах, ні користувачу.
        if getattr(message, "stop_reason", None) == "max_tokens":
            raise HTTPException(
                status_code=503,
                detail="Pattern too large to generate. Try a simpler idea or a smaller size.",
            )

        text = _strip_code_fences(message.content[0].text)
        pattern = json.loads(text)
        pattern = fix_chart_types(pattern)

        return {"pattern": pattern}

    except json.JSONDecodeError:
        # Кредит уже списано до виклику моделі, тож користувач за цю генерацію
        # заплатив. Даємо ще одну спробу за наш рахунок замість того, щоб
        # повертати помилку на оплачений запит.
        try:
            retry_message = client.messages.create(
                model=model,
                max_tokens=16384,
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": f"Design a crochet pattern.\\nIdea: {request_body.idea}\\nDifficulty: {request_body.difficulty}\\nSize / scale: {request_body.size}\\nIMPORTANT: Use {request_body.units} for ALL measurements. Keep the pattern compact: no more than 8 sections. Return ONLY the JSON object, nothing else."
                    }
                ],
            )
            if getattr(retry_message, "stop_reason", None) == "max_tokens":
                raise HTTPException(
                    status_code=503,
                    detail="Pattern too large to generate. Try a simpler idea or a smaller size.",
                )
            pattern = json.loads(_strip_code_fences(retry_message.content[0].text))
            pattern = fix_chart_types(pattern)
            return {"pattern": pattern}
        except HTTPException:
            raise
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=502,
                detail=f"Could not build a valid pattern this time. Please try again. ({e.msg})",
            )'''
    if old not in src:
        shutil.move(str(backup), str(MAIN))
        fail("не знайдено блок парсингу відповіді моделі.")
    src = src.replace(old, new, 1)
    changes += 1
    print("  + обрив по стелі токенів діагностується явно (503)")
    print("  + одна безкоштовна повторна спроба при зламаному JSON")

    # ── 5. Надійне зрізання ```-фенсів ──────────────────────────────────────
    old = "def get_user_profile(authorization: str):"
    new = '''def _strip_code_fences(raw: str) -> str:
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


def get_user_profile(authorization: str):'''
    src = src.replace(old, new, 1)
    changes += 1
    print("  + зрізання ```-фенсів більше не падає з IndexError")

    MAIN.write_text(src, encoding="utf-8")
    print(f"\n  Внесено змін: {changes}")

    print("  Перевіряю синтаксис...")
    try:
        py_compile.compile(str(MAIN), doraise=True)
    except py_compile.PyCompileError as exc:
        shutil.move(str(backup), str(MAIN))
        print(f"\n  Синтаксис НЕ пройшов — оригінал повернуто:\n{exc}\n")
        return 1

    print("  Синтаксис OK.\n")
    print("  Далі:")
    print("    git add main.py && git commit -m 'fix: JWT 401, rate-limit key, retry cost, token cap'")
    print("    git push        # Render підхопить і передеплоїть\n")
    print(f"  Резервна копія лишилась: {backup.name}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
