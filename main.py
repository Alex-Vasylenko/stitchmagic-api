from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
import anthropic
from supabase import create_client, Client
import httpx
import os
import json
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://magic-crochet-bot.lovable.app"],
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


def get_user_profile(authorization: str):
    token = authorization.replace("Bearer ", "")
    authed_client: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    authed_client.postgrest.auth(token)
    user_resp = authed_client.auth.get_user(token)
    if not user_resp.user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user_resp.user.id
    profile_resp = authed_client.table("profiles").select("*").eq("user_id", user_id).single().execute()
    if not profile_resp.data:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile_resp.data


def increment_generations(authorization: str, amount: float = 1.0):
    """Викликає Edge Function для безпечного списування генерацій"""
    response = httpx.post(
        EDGE_FUNCTION_URL,
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json",
        },
        json={"amount": amount},
        timeout=10.0,
    )
    if response.status_code == 402:
        raise HTTPException(status_code=402, detail="Generation limit reached")
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to update generations")
    return response.json()


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


ALLOWED_DIFFICULTIES = {"Beginner", "Easy", "Intermediate", "Advanced"}
ALLOWED_UNITS = {"cm", "inches"}


class GenerateRequest(BaseModel):
    idea: str = Field(..., min_length=3, max_length=500)
    difficulty: str = "Easy"
    size: str = Field(default="Standard", max_length=100)
    units: str = "cm"

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
def generate_pattern(request: GenerateRequest, authorization: str = Header(...)):
    # Перевіряємо ліміт і списуємо генерацію через Edge Function
    increment_generations(authorization, amount=1.0)

    # Читаємо план для вибору моделі
    profile = get_user_profile(authorization)
    plan = profile.get("plan", "free")
    model = PLAN_MODELS.get(plan, "claude-haiku-4-5-20251001")

    try:
        message = client.messages.create(
            model=model,
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
    result = anon_client.table("profiles").select("id", count="exact").eq("plan", "founder").execute()
    used = result.count or 0
    return {"slots_remaining": max(0, 100 - used)}


@app.get("/health")
def health():
    return {"status": "ok"}
