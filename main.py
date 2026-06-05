from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic
import os
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


def fix_chart_types(pattern: dict) -> dict:
    """
    Post-process chart section types based on actual row instructions.
    Deterministic override — runs after Haiku generates the pattern.

    Rules (checked in order):
      1. round 1 notes contain 'magic ring' → type = 'round'
      2. any row instruction contains 'turn' → type = 'flat'
      3. everything else                     → type = 'cylinder'
    """
    try:
        chart = pattern.get("chart")
        if not chart or not isinstance(chart, dict):
            return pattern

        sections = chart.get("sections")
        if not isinstance(sections, list):
            return pattern

        # Build lookup of human-readable instructions per section name
        section_instructions: dict[str, list[str]] = {}
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

            # Check 1: magic ring in round 1 notes
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

            # Check 2: 'turn' in human-readable instructions
            has_turn = False
            for instr in section_instructions.get(name, []):
                if "turn" in instr:
                    has_turn = True
                    break

            # Also check round notes for 'turn' as fallback
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

            # Check 3: default
            section["type"] = "cylinder"

    except Exception:
        # Never crash the request due to post-processing
        pass

    return pattern


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
  tc (treble crochet), sl st (slip stitch), ch (chain),
  inc (increase = 2sc in same st), dec (decrease = sc2tog),
  fpdc (front post dc), bpdc (back post dc),
  bobble, shell, cluster, picot

CHART RULES:
- increases array: list the INDEX positions where inc stitches occur in that round
- decreases array: list the INDEX positions where dec stitches occur
- shape_change per round: expanding if stitch count grows, decreasing if it shrinks, straight if same as previous
- notes: any special instruction for that round (magic ring, fasten off, stuff before closing etc)
- Be precise about increase/decrease positions - they must match the symbols array
- For chart type follow this STRICT decision tree — check in this exact order:

  STEP 1: Does round 1 notes contain "magic ring"?
          YES → type = "round". STOP. Do not check anything else.
          
  STEP 2: Does any row instruction contain "turn"?
          YES → type = "flat". STOP.
          
  STEP 3: Everything else → type = "cylinder".

  CRITICAL: "magic ring" in notes ALWAYS means "round", even if the piece is called
  "body", "head", "tail", or anything else. Never override this with "cylinder".
  
  Correct examples:
  - Whale Body, round 1 notes = "magic ring, 6 sc" → "round"
  - Whale Head, round 1 notes = "magic ring, 6 sc" → "round"
  - Pectoral Fin, has "turn" in rows → "flat"
  - Dorsal Fin, has "turn" in rows → "flat"
  - Hat body, no magic ring, no turn, continuous spiral → "cylinder"

- For square type, mark corner positions in the symbols array with "corner" symbol
- For cone type, show expanding or decreasing circles proportionally
- For triangle type, show rows that increase or decrease on one or both sides

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
    },
    {
      "name": "Accent color",
      "hex": "#hexcolor",
      "description": "secondary color for details if needed"
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

class GenerateRequest(BaseModel):
    idea: str
    difficulty: str = "Easy"
    size: str = "Standard"

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
                    "content": f"Design a crochet pattern.\nIdea: {request.idea}\nDifficulty: {request.difficulty}\nSize / scale: {request.size}\n\nReturn ONLY the JSON object."
                }
            ]
        )

        text = message.content[0].text.strip()

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        pattern = json.loads(text)

        # Post-process: deterministically fix chart section types
        pattern = fix_chart_types(pattern)

        return {"pattern": pattern}

    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Invalid JSON from Claude: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health():
    return {"status": "ok"}
