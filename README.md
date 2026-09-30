# TheEconmaster Engine

מנוע רינדור חינמי לסרטונים ארוכים (GitHub Actions + FFmpeg).

זרימה: Make בונה תסריט ← אתה מאשר בטלגרם ← Make שולח לכאן ← כאן נוצרים קול, ויזואל, כתוביות, תמונה ממוזערת ← Make מעלה ליוטיוב כפרטי.

## קבצים
- `render.py` – המנוע.
- `.github/workflows/render.yml` – ההפעלה בענן.
- `prompts.md` – הפרומפטים למחקר ולתסריט.
- `fonts/` – פונטים חינמיים (Anton, Inter).
- `music/` – לשים כאן 5-10 קבצי mp3 מ-YouTube Audio Library (לא חובה).

## סודות (Settings > Secrets and variables > Actions)
- `ELEVENLABS_API_KEY`
- `ELEVENLABS_VOICE_ID`
- `PEXELS_API_KEY`
- `MAKE_WEBHOOK_URL`

משתנה (Variables): `TARGET_MB` = 95 לתוכנית Make Core, או 240 לתוכנית Pro.
