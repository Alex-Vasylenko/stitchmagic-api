# StitchMagic API

FastAPI backend for StitchMagic crochet pattern generator using Claude AI.

## Endpoints

- `GET /` — API status
- `GET /health` — Health check  
- `POST /api/generate` — Generate crochet pattern

## Deploy on Render

1. Connect this GitHub repo to Render
2. Add environment variable: `ANTHROPIC_API_KEY`
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
