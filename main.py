from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic
import os
import json
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


def expand_symbols(symbols: list) -> list:
    """
    Розгортає скорочені описи петель у список символів.
    Наприклад:
      "3 dc"                       → ["dc", "dc", "dc"]
      "ch-2 sp"                    → ["ch", "ch"]
      "[3 dc, ch 2, 3 dc] in corner" → ["dc","dc","dc","ch","ch","dc","dc","dc"]
      "dc in next 3 st"            → ["dc","dc","dc"]
    """
    KNOWN = {
        'sc', 'dc', 'hdc', 'tr', 'ch', 'sl', 'mr', 'inc', 'dec',
        'fpdc', 'bpdc', 'shell', 'bobble', 'cluster', 'picot', 'slst'
    }
    # Довші варіанти першими — щоб "slst" не розпізнався як "sl"
    KNOWN_ORDERED = ['fpdc', 'bpdc', 'hdc', 'slst', 'shell', 'bobble', 'cluster', 'picot',
                     'sc', 'dc', 'tr', 'ch', 'sl', 'mr', 'inc', 'dec']

    def parse_one(raw):
        raw = str(raw).strip()
        lower = raw.lower().replace('sl st', 'slst').replace('slip stitch', 'slst')

        # magic ring → mr
        if 'magic ring' in lower:
            return ['mr']

        has_digits = bool(re.search(r'\d', lower))

        if not has_digits:
            # Немає цифр — шукаємо відомі символи як цілі слова
            result = []
            for k in KNOWN_ORDERED:
                if re.search(r'\b' + k + r'\b', lower):
                    result.append('sl' if k == 'slst' else k)
            return result if result else []

        # Є цифри — збираємо всі (позиція, кількість, символ)
        found = []

        # Pattern A: digit → stitch  e.g. "3 dc", "3-dc", "3dc"
        for m in re.finditer(r'(\d+)\s*[-]?\s*([a-z]+)', lower):
            stitch = m.group(2)
            if stitch == 'slst': stitch = 'sl'
            if stitch in KNOWN or stitch == 'sl':
                found.append((m.start(), int(m.group(1)), stitch))

        # Pattern B: stitch → digit впритул  e.g. "ch 2", "ch-2"
        for m in re.finditer(r'([a-z]+)\s*[-]?\s*(\d+)', lower):
            stitch = m.group(1)
            if stitch == 'slst': stitch = 'sl'
            if stitch in KNOWN or stitch == 'sl':
                found.append((m.start(), int(m.group(2)), stitch))

        # Pattern D: stitch ... digit без коми між ними  e.g. "dc in next 3 st"
        # Важливо: ПЕРЕД Pattern C, щоб мати пріоритет над одиночним символом
        for m in re.finditer(
            r'\b(sc|dc|hdc|tr|ch|sl|inc|dec|fpdc|bpdc)\b([^,\[\]]{1,20}?)(\d+)\s*(?:st|sp|times|sts|x)?\b',
            lower
        ):
            stitch = m.group(1)
            if stitch == 'slst': stitch = 'sl'
            found.append((m.start(), int(m.group(3)), stitch))

        # Pattern C: самотній символ без сусідньої цифри впритул
        # Йде після D, але при dedup D перемагає завдяки більшому count
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
            # При однаковій позиції — більший count перемагає (Pattern D > Pattern C)
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
            # Розгортаємо symbols у кожному раунді
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
- Stitch counts MUST be mathematically correct. Double-check every increase/decrease round.
- Use standard US crochet terminology and abbreviations.
- Every row/round must have a stitch count in parentheses.
- Include gauge, materials, and finished measurements.
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

CHART RULES:
- increases array: list the INDEX positions where inc stitches occur in that round
- decreases array: list the INDEX positions where dec stitches occur
- shape_change per round: expanding if stitch count grows, decreasing if it shrinks, straight if same as previous
- notes: any special instruction for that round (magic ring, fasten off, stuff before closing etc)
- Be precise about increase/decrease positions - they must match the symbols array
- CRITICAL: symbols array MUST contain individual stitch codes only.
  Each element = one stitch. Use: sc, dc, hdc, tr, ch, sl, inc, dec, fpdc, bpdc, mr
  NEVER put descriptions like "3 dc in ring" or "ch-2 sp" as single elements.
  CORRECT: ["sc","sc","sc","dc","dc","ch","ch"]
  WRONG:   ["3 sc", "2 dc in ring", "ch-2 sp"]
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
    "yarn_weight": "weight",
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
            "round": 1,
            "stitch_count": 6,
            "shape_change": "expanding",
            "color_name": "Main color",
            "symbols": ["sc","sc","sc","sc","sc","sc"],
            "increases": [0, 2, 4],
            "decreases": [],
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


class GenerateRequest(BaseModel):
    idea: str
    difficulty: str = "Easy"
    size: str = "Standard"
    units: str = "cm"


class SvgRequest(BaseModel):
    title: str
    colors: list


@app.get("/")
def root():
    return {"status": "StitchMagic API is running", "model": "claude-haiku-4-5-20251001"}


@app.post("/api/generate")
def generate_pattern(request: GenerateRequest):
    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Design a crochet pattern.\nIdea: {request.idea}\nDifficulty: {request.difficulty}\nSize / scale: {request.size}\nIMPORTANT: Use {request.units} for ALL measurements. Gauge must be in {request.units}. Finished size must be in {request.units}. Do not use any other unit of measurement.\n\nReturn ONLY the JSON object."
                }
            ]
        )

        text = message.content[0].text.strip()

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        pattern = json.loads(text)
        pattern = fix_chart_types(pattern)

        return {"pattern": pattern}

    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Invalid JSON from Claude: {str(e)}")
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


@app.get("/health")
def health():
    return {"status": "ok"}
