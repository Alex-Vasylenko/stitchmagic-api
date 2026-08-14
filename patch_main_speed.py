#!/usr/bin/env python3
"""
main.py — усунення обривів і скорочення часу генерації.

Симптом: складний патерн генерується 5-6 хвилин і зривається без відповіді.

Дві причини:

1. Запит до моделі йде БЕЗ стріму. Поки відповідь не готова цілком, по
   з'єднанню не йде жодного байта — проміжний проксі рве таке з'єднання по
   таймауту простою. Чим довша генерація, тим вища ймовірність обриву.
   Виправлення: messages.stream(). Байти течуть постійно, з'єднання живе.

2. Промпт вимагає перелічувати КОЖНУ петлю окремим елементом масиву. Для
   подушки це ~2300 елементів — більша частина всієї відповіді. При цьому в
   цьому ж файлі вже є expand_symbols(), яка розгортає компактний запис
   "26 sc" у ті самі 26 петель. Тобто модель вручну пише те, що бекенд і так
   уміє розгорнути.
   Виправлення: дозволити компактний запис. Відповідь коротшає в рази,
   генерація пропорційно швидшає, ризик упертись у стелю токенів падає.

Запуск із папки репозиторію stitchmagic-api:
    python patch_main_speed.py
"""
import py_compile
import shutil
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parent / "main.py"

HELPER = '''def _stream_message(**kwargs):
    """
    Виклик моделі зі стрімом.

    Без стріму з'єднання мовчить до самого кінця генерації, і проміжний проксі
    рве його по таймауту простою — саме звідси обриви на 5-6 хвилині. Зі стрімом
    дані течуть постійно, тож з'єднання лишається живим скільки треба.

    Повертає готовий Message, тож решта коду (перевірка stop_reason, читання
    content[0].text) працює без змін.
    """
    with client.messages.stream(**kwargs) as stream:
        return stream.get_final_message()


def get_user_profile(authorization: str):'''

OLD_SYMBOLS = '''- CRITICAL: symbols array MUST contain individual stitch codes only.
  Each element = one stitch. Use: sc, dc, hdc, tr, ch, sl, inc, dec, fpdc, bpdc, mr
  NEVER put descriptions like "3 dc in ring" or "ch-2 sp" as single elements.
  CORRECT: ["sc","sc","sc","dc","dc","ch","ch"]
  WRONG:   ["3 sc", "2 dc in ring", "ch-2 sp"]'''

NEW_SYMBOLS = '''- symbols array: use COMPACT run-length notation. Write "<count> <stitch>" for
  consecutive identical stitches instead of repeating them one by one. The
  server expands this automatically, so a round of 26 single crochets is
  ["26 sc"], not twenty-six separate entries.
  Allowed stitch codes: sc, dc, hdc, tr, ch, sl, inc, dec, fpdc, bpdc, mr
  CORRECT: ["mr", "6 sc"]
  CORRECT: ["dec", "22 sc", "dec"]
  CORRECT: ["3 sc", "inc", "3 sc", "inc"]
  WRONG:   ["sc","sc","sc","sc","sc","sc", ...twenty more...]
  WRONG:   ["2 dc in ring", "ch-2 sp"]   (prose, not a stitch code)
  Keep the total stitch count implied by symbols equal to stitch_count.'''


def main():
    print("\n  Патч: стрім і компактні символи\n  " + "=" * 32 + "\n")
    if not MAIN.exists():
        print(f"  ПОМИЛКА: не знайдено {MAIN}\n")
        return 1

    src = MAIN.read_text(encoding="utf-8")
    if "_stream_message" in src:
        print("  Уже пропатчено.\n")
        return 0

    backup = MAIN.with_suffix(".py.bak_speed")
    shutil.copy2(MAIN, backup)
    print(f"  Резервна копія: {backup.name}")
    n = 0

    # 1. Хелпер зі стрімом
    if "def get_user_profile(authorization: str):" not in src:
        shutil.move(str(backup), str(MAIN))
        print("  ПОМИЛКА: не знайдено get_user_profile\n")
        return 1
    src = src.replace("def get_user_profile(authorization: str):", HELPER, 1)
    n += 1
    print("  + хелпер _stream_message()")

    # 2. Обидва виклики генерації переводимо на стрім
    before = src
    src = src.replace("message = client.messages.create(", "message = _stream_message(", 1)
    src = src.replace("retry_message = client.messages.create(",
                      "retry_message = _stream_message(", 1)
    if src != before:
        n += 1
        print("  + генерація патерна йде через стрім")
    # generate_svg лишається без стріму: max_tokens=1000, секунди

    # 3. Компактні символи в промпті
    if OLD_SYMBOLS in src:
        src = src.replace(OLD_SYMBOLS, NEW_SYMBOLS, 1)
        n += 1
        print("  + компактний запис символів у діаграмах")
    else:
        print("  ! блок про symbols виглядає інакше — пропущено")

    # 4. Приклад у структурі JSON
    old_example = '"symbols": ["sc","sc","sc","sc","sc","sc"],'
    if old_example in src:
        src = src.replace(old_example, '"symbols": ["mr", "6 sc"],', 1)
        n += 1
        print("  + приклад у структурі JSON оновлено")

    MAIN.write_text(src, encoding="utf-8")
    print(f"\n  Внесено змін: {n}")
    print("  Перевіряю синтаксис...")
    try:
        py_compile.compile(str(MAIN), doraise=True)
    except py_compile.PyCompileError as exc:
        shutil.move(str(backup), str(MAIN))
        print(f"  НЕ пройшло — оригінал повернуто:\n{exc}\n")
        return 1

    print("  Синтаксис OK.\n")
    print("    git add main.py")
    print('    git commit -m "perf: stream model call, compact chart symbols"')
    print("    git push\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
