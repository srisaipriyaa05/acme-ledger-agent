# Acme Appliances Ledger Agent — Vercel

## Deploy
1. Upload the contents of this folder to the root of a GitHub repository (the `app/` directory, `data.ndjson`, and `requirements.txt` must be at the repository root level).
2. Import the GitHub repository at https://vercel.com/new.
3. Keep the project root as `./`. Vercel should detect the FastAPI app at `app/main.py` automatically.
4. Deploy. No custom build or start command is needed for Vercel's FastAPI runtime.
5. After deployment, test `https://YOUR-PROJECT.vercel.app/healthz` and send a POST to `/` with JSON `{"question":"How many paid orders are there?"}`.

## Environment variables
- `RATES_URL`: set to your personalized `/rates` endpoint if you need currency conversions. Verify its returned structure before relying on conversions.
- `AIPIPE_TOKEN`: optional token for AI-assisted question interpretation. Add only in Vercel Project Settings → Environment Variables, never in source control.
- `AI_MODEL`: optional; defaults to `openrouter/free`.

The project loads `data.ndjson` at startup. The data snapshot is specific to the assignment. The app deduplicates by latest `updated_at`; only current `paid` rows count as revenue. Dates use Asia/Kolkata.
