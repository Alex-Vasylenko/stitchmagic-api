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

SYSTEM_PROMPT = """You are an expert crochet pattern designer. When given a description, generate a complete, accurate crochet pattern in strict JSON format.

CRITICAL RULES:
- Stitch counts MUST be mathematically correct. Double-check every increase/decrease round.
- Use standard US crochet terminology and abbreviations.
- Every row/round must have a stitch count in parentheses.
- Include gauge, materials, and finished measurements.
- Yarn yardage estimates must be realistic for the item and yarn weight.
- Patterns must be suitable for the stated difficulty level.

RESPOND WITH ONLY VALID JSON — no markdown, no explanation, no code fences.

JSON structure:
{
  "title": "Pattern name",
  "difficulty": "Easy|Medium|Hard",
  "finished_size": "dimensions",
  "gauge": "X sc = X inches",
  "materials": {
    "yarn_weight": "weight",
    "yarn_yardage": number,
    "hook_size": "size",
    "extras": ["item1", "item2"]
  },
  "color_hex": "#hexcolor",
  "svg_type": "beanie|sweater|scarf|amigurumi|bag|blanket|socks|mittens|toy",
  "sections": [
    {
      "name": "Section name",
      "rows": [
        {
          "id": "row_1",
          "row_number": 1,
          "instruction": "full instruction here",
          "stitch_count": number
        }
      ]
    }
  ],
  "assembly": ["step1", "step2"]
}"""

class GenerateRequest(BaseModel):
    idea: str
    difficulty: str = "Easy"
    size: str = "Standard"

@app.get("/")
def root():
    return {"status": "StitchMagic API is running", "model": "claude-sonnet-4-20250514"}

@app.post("/api/generate")
def generate_pattern(request: GenerateRequest):
    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
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
        return {"pattern": pattern}
        
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Invalid JSON from Claude: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health():
    return {"status": "ok"}
