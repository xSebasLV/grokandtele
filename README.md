# Radar Freelance + Empleo (serverless, sin n8n)

Busca oportunidades en 11 plataformas cada 2 horas, las califica con IA (Groq → NIM → GLM, todo gratis),
guarda las de score ≥ 6 en `data/leads.csv` y te avisa por Telegram las de score ≥ 8.
Corre solo en **GitHub Actions**: sin servidor, sin PC prendido, sin suscripción.

## Puesta en marcha (una sola vez, ~10 min)

### 1. Crear el repositorio
1. En GitHub: **New repository** → nombre p.ej. `radar-freelance` → **Private** → Create.
2. Sube estos archivos (arrastra la carpeta en "uploading an existing file", o con git):
   ```
   git init
   git add .
   git commit -m "radar inicial"
   git branch -M main
   git remote add origin https://github.com/TU_USUARIO/radar-freelance.git
   git push -u origin main
   ```

### 2. Cargar las claves (Secrets)
En el repo → **Settings → Secrets and variables → Actions → New repository secret**.
Crea estos (el nombre EXACTO, en mayúsculas):

| Secret | De dónde sale |
|---|---|
| `GROQ_API_KEY` | console.groq.com → API Keys (gratis, sin tarjeta) |
| `NVIDIA_API_KEY` | build.nvidia.com → Get API Key (la que ya usabas) |
| `GLM_API_KEY` | z.ai → API Keys (gratis) |
| `TELEGRAM_TOKEN` | Telegram → @BotFather → /newbot |
| `TELEGRAM_CHAT_ID` | Telegram → @userinfobot (tu número). ⚠️ Dale **Start** a tu bot primero |
| `FREELANCER_TOKEN` | *(opcional)* developers.freelancer.com |

Con tener **una** de las 3 de IA basta; las otras son respaldo.

### 3. Activar
- Pestaña **Actions** → si pide habilitar workflows, acepta.
- Para probar ya: **Actions → Radar Freelance + Empleo → Run workflow**.
- Revisa el log; si todo va bien, empezará a correr solo cada 2 h.

## Dónde ves los resultados
- **Telegram:** aviso automático de las mejores (score ≥ 8).
- **`data/leads.csv`:** todas las de score ≥ 6. Ábrelo en el repo, o descárgalo y ábrelo en Excel / Google Sheets.
- **`data/seen.txt`:** memoria de lo ya visto (para no repetir). No lo toques.

## Ajustes rápidos
- **Qué busca:** edita la lista `KEYWORDS` arriba en `radar.py`.
- **Qué tan exigente:** `SCORE_GUARDAR` (guardar) y `SCORE_AVISAR` (avisar por Telegram).
- **Cada cuánto corre:** la línea `cron` en `.github/workflows/radar.yml` (`0 */2 * * *` = cada 2 h).
- **Modelos de IA:** `GROQ_MODEL`, `NIM_MODEL`, `GLM_MODEL` en `radar.py` (si rotan los nombres).

## Notas
- GitHub desactiva los cron si el repo pasa **60 días sin actividad**; como hace commit de resultados, se mantiene vivo solo.
- Los horarios del cron son en **UTC** y pueden retrasarse unos minutos bajo carga: normal.
- Workana es scraping (la fuente más frágil); si algún día cambia su HTML, se salta sola sin romper el resto.
