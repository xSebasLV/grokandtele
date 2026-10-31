#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RADAR FREELANCE + EMPLEO  -  versión serverless (sin n8n)
Corre en GitHub Actions cada 2h. 11 fuentes -> dedup local -> IA (Groq->NIM->GLM) -> Telegram + CSV.

Secrets que lee del entorno (GitHub -> Settings -> Secrets and variables -> Actions):
  GROQ_API_KEY       (1a opcion de IA, gratis en console.groq.com)
  NVIDIA_API_KEY     (2a opcion, build.nvidia.com)
  GLM_API_KEY        (3a opcion, z.ai)
  TELEGRAM_TOKEN     (BotFather)
  TELEGRAM_CHAT_ID   (@userinfobot)
  FREELANCER_TOKEN   (opcional, developers.freelancer.com)
"""

import os, re, csv, json, time, html, datetime, pathlib
import requests

# ============================================================
#  CONFIG  -  ajusta a tus servicios reales
# ============================================================
KEYWORDS = [
    'dashboard','power bi','powerbi','data analysis','analista de datos','analisis de datos',
    'web development','desarrollo web','wordpress','elementor','php','laravel','mysql',
    'sistema de informacion','software a medida','crm','erp','inventario','landing page',
    'automation','automatizacion','n8n','make.com','scraping','sql','looker','tableau','etl',
]

# Filtro mas amplio para fuentes FREELANCE (Reddit, HackerNews, Workana), donde los
# anuncios usan lenguaje libre ("necesito que me construyan un sistema"). La IA hace el
# filtro fino despues; aqui solo descartamos lo obviamente ajeno.
GENERIC_DEV = [
    'develop','desarroll','program','website','web ','web,','web.','pagina','página','sitio',
    'data','dato','dashboard','excel','sheet','sql','base de datos','database','api','automat',
    'bot','script','scrap','wordpress','php','python','laravel','javascript','node','software',
    'sistema','app','aplicaci','integrat','integraci','crm','erp','landing','ecommerce','shopify',
    'seo','power bi','analytics','analitic','report','scraper',
]

SCORE_GUARDAR  = 6   # se guarda en el CSV si score >= esto
SCORE_AVISAR   = 6   # se avisa por Telegram si score >= esto (subelo si te llega mucho)

# Cuantos leads calificar por corrida. Evita reventar los limites por minuto de la IA.
# Los que sobran NO se marcan vistos: se procesan en la siguiente corrida (cada 2h).
MAX_POR_CORRIDA = 40
PAUSA_IA_S      = 1.5   # pausa entre llamadas a la IA

# Keys desde entorno
GROQ_API_KEY     = os.environ.get('GROQ_API_KEY', '')
GEMINI_API_KEY   = os.environ.get('GEMINI_API_KEY', '')
GLM_API_KEY      = os.environ.get('GLM_API_KEY', '')
NVIDIA_API_KEY   = os.environ.get('NVIDIA_API_KEY', '')
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
FREELANCER_TOKEN = os.environ.get('FREELANCER_TOKEN', '')
FREELANCER_USER_ID = os.environ.get('FREELANCER_USER_ID', '')

# --- Auto-aplicar a Freelancer.com ---
# AUTO_BID=1  -> envia la oferta SOLO (gasta tus bids del mes). Default: 0 = modo revision
#               (manda la propuesta a Telegram para que apliques tu).
# Requiere FREELANCER_TOKEN (con permiso de bid). Solo aplica a score >= AUTO_BID_MIN.
def _int_env(name, default):
    # GitHub pasa "" cuando el Secret no existe -> int("") reventaria. Tolerar vacio.
    v = (os.environ.get(name) or '').strip()
    try: return int(v) if v else default
    except ValueError: return default

AUTO_BID     = (os.environ.get('AUTO_BID') or '').strip() == '1'
AUTO_BID_MIN = _int_env('AUTO_BID_MIN', 9)   # umbral de score para aplicar
AUTO_BID_MAX = _int_env('AUTO_BID_MAX', 3)   # tope de ofertas por corrida

# Cadena de IA: se intenta en orden. El 1ro con key que responda gana.
# Si uno se satura (rate limit), se desactiva por esta corrida y se pasa al siguiente.
# Todos gratis y formato OpenAI, menos Gemini (formato propio, kind='gemini').
PROVIDERS = [
    {'name':'groq',   'kind':'openai', 'key':GROQ_API_KEY,   'model':'openai/gpt-oss-20b',
     'url':'https://api.groq.com/openai/v1/chat/completions'},
    {'name':'gemini', 'kind':'gemini', 'key':GEMINI_API_KEY, 'model':'gemini-2.0-flash',
     'url':'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent'},
    {'name':'glm',    'kind':'openai', 'key':GLM_API_KEY,    'model':'glm-4.7-flash',
     'url':'https://api.z.ai/api/paas/v4/chat/completions'},
    {'name':'nim',    'kind':'openai', 'key':NVIDIA_API_KEY, 'model':'meta/llama-3.3-70b-instruct',
     'url':'https://integrate.api.nvidia.com/v1/chat/completions'},
]
AGOTADOS = set()   # proveedores que se saturaron en esta corrida

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

DATA_DIR  = pathlib.Path(__file__).parent / 'data'
SEEN_FILE = DATA_DIR / 'seen.txt'
CSV_FILE  = DATA_DIR / 'leads.csv'
DATA_DIR.mkdir(exist_ok=True)

SYSTEM = (
    "Eres el filtro de oportunidades de Impulsate / PCMarkett (Bogota, Colombia). "
    "El usuario busca proyectos freelance y un posible segundo empleo remoto. "
    "PERFIL: desarrollo web (WordPress, Elementor, PHP, Laravel), sistemas de informacion a medida "
    "(inventarios, CRM, ERP), analisis de datos y dashboards (Power BI, DAX, SQL, MySQL, Python), "
    "automatizacion (n8n, APIs, scraping), SEO y marketing digital.\n"
    "RUBRICA de score (0-10):\n"
    "9-10: pide exactamente un servicio core (dashboard/Power BI, web WordPress/PHP/Laravel, "
    "sistema a medida, automatizacion n8n, analisis de datos).\n"
    "7-8: claramente dentro del area aunque no nombre la herramienta exacta (analisis/visualizacion "
    "de datos en general, SQL, desarrollo web o backend, integraciones, ETL).\n"
    "5-6: relacionado o tangencial (TI general, soporte de datos, automatizacion ligera, WordPress basico).\n"
    "0-4: fuera del perfil (apps moviles nativas, blockchain, ML pesado, 3D, redaccion pura, diseno grafico puro).\n"
    "Ante la duda entre dos niveles, elige el mas alto si menciona datos, dashboards, web o automatizacion. "
    "Devuelve SOLO un objeto JSON valido (sin markdown) con llaves: "
    "score (entero 0-10), motivo (string max 20 palabras en espanol), "
    "pitch (2 frases en el idioma del anuncio, apertura personalizada que mencione algo concreto del anuncio)."
)

# ============================================================
#  HELPERS
# ============================================================
def clean(s):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', ' ', s or '')).strip()

def match_kw(txt):
    t = (txt or '').lower()
    return any(k in t for k in KEYWORDS)

def match_dev(txt):
    t = (txt or '').lower()
    return any(k in t for k in GENERIC_DEV)

def get_json(url, headers=None, timeout=25):
    h = {'User-Agent': UA}
    if headers: h.update(headers)
    return requests.get(url, headers=h, timeout=timeout).json()

def get_text(url, headers=None, timeout=25):
    h = {'User-Agent': UA}
    if headers: h.update(headers)
    return requests.get(url, headers=h, timeout=timeout).text

def load_seen():
    if SEEN_FILE.exists():
        return set(l.strip() for l in SEEN_FILE.read_text(encoding='utf-8').splitlines() if l.strip())
    return set()

# ============================================================
#  RECOLECTOR  (11 fuentes)
# ============================================================
def recolectar(seen):
    out = []
    def push(o):
        u = (o.get('url') or '').strip()
        if not u or u in seen: return
        seen.add(u)
        out.append(o)

    # 1. RemoteOK
    try:
        jobs = get_json('https://remoteok.com/api')[1:]
        for j in jobs:
            blob = f"{j.get('position','')} {j.get('description','')} {' '.join(j.get('tags',[]))}"
            if not match_kw(blob): continue
            push({'tipo':'empleo','fuente':'RemoteOK','titulo':j.get('position',''),'empresa':j.get('company',''),
                  'descripcion':clean(j.get('description',''))[:1200],
                  'presupuesto':(f"{j.get('salary_min')}-{j.get('salary_max')} USD/anio" if j.get('salary_min') else ''),
                  'url':j.get('url') or f"https://remoteok.com/l/{j.get('id')}"})
    except Exception as e: print('RemoteOK:', e)

    # 2. Remotive
    for kw in ['dashboard','power bi','php','wordpress','data analyst','laravel']:
        try:
            r = get_json(f'https://remotive.com/api/remote-jobs?search={requests.utils.quote(kw)}&limit=40')
            for j in r.get('jobs', []):
                push({'tipo':'empleo','fuente':'Remotive','titulo':j.get('title',''),'empresa':j.get('company_name',''),
                      'descripcion':clean(j.get('description',''))[:1200],'presupuesto':j.get('salary',''),'url':j.get('url','')})
        except Exception as e: print('Remotive:', e)

    # 3. Jobicy
    for kw in ['dashboard','php','wordpress','data','laravel']:
        try:
            r = get_json(f'https://jobicy.com/api/v2/remote-jobs?count=40&tag={requests.utils.quote(kw)}')
            for j in r.get('jobs', []):
                blob = f"{j.get('jobTitle','')} {j.get('jobDescription','')}"
                if not match_kw(blob): continue
                push({'tipo':'empleo','fuente':'Jobicy','titulo':j.get('jobTitle',''),'empresa':j.get('companyName',''),
                      'descripcion':clean(j.get('jobDescription',''))[:1200],
                      'presupuesto':(f"{j.get('salaryMin')}-{j.get('salaryMax')} {j.get('salaryCurrency','USD')}" if j.get('salaryMin') else ''),
                      'url':j.get('url','')})
        except Exception as e: print('Jobicy:', e)

    # 4. Arbeitnow
    try:
        r = get_json('https://www.arbeitnow.com/api/job-board-api')
        for j in r.get('data', []):
            blob = f"{j.get('title','')} {j.get('description','')} {' '.join(j.get('tags',[]))}"
            if not match_kw(blob): continue
            push({'tipo':'empleo','fuente':'Arbeitnow','titulo':j.get('title',''),'empresa':j.get('company_name',''),
                  'descripcion':clean(j.get('description',''))[:1200],'presupuesto':'','url':j.get('url','')})
    except Exception as e: print('Arbeitnow:', e)

    # 5. WeWorkRemotely (RSS)
    for f in ['https://weworkremotely.com/categories/remote-programming-jobs.rss',
              'https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss',
              'https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss']:
        try:
            xml = get_text(f)
            for it in xml.split('<item>')[1:]:
                def pick(tag):
                    m = re.search('<'+tag+'>(.*?)</'+tag+'>', it, re.S)
                    if not m: return ''
                    return m.group(1).replace('<![CDATA[','').replace(']]>','').strip()
                titulo = clean(pick('title')); desc = clean(pick('description'))
                if not match_kw(titulo+' '+desc): continue
                push({'tipo':'empleo','fuente':'WeWorkRemotely','titulo':titulo,'empresa':'',
                      'descripcion':desc[:1200],'presupuesto':'','url':pick('link')})
        except Exception as e: print('WWR:', e)

    # 6. Reddit
    # Reddit exige un User-Agent descriptivo y unico, o bloquea desde datacenter (GitHub).
    reddit_ua = {'User-Agent':'radar-freelance/1.0 (by /u/xSebasLV)'}
    for sub in ['forhire','jobbit','freelance_forhire','slavelabour']:
        try:
            r = get_json(f'https://www.reddit.com/r/{sub}/new.json?limit=100', headers=reddit_ua)
            for p in r.get('data',{}).get('children', []):
                d = p.get('data', {})
                titulo = d.get('title','') or ''
                tl = titulo.lower()
                # Quedarnos con los que CONTRATAN (no los [for hire] que ofrecen servicio).
                if '[hiring]' not in tl and '[task]' not in tl: continue
                blob = f"{titulo} {d.get('selftext','')}"
                if not match_dev(blob): continue   # filtro amplio; la IA decide el fino
                push({'tipo':'freelance','fuente':f'Reddit r/{sub}','titulo':titulo,'empresa':d.get('author',''),
                      'descripcion':(d.get('selftext','') or '')[:1200],'presupuesto':'',
                      'url':'https://www.reddit.com'+d.get('permalink','')})
        except Exception as e: print('Reddit', sub, e)

    # 7. HackerNews (Who is hiring / Freelancer seeking)
    try:
        s = get_json('https://hn.algolia.com/api/v1/search?query=freelancer%20seeking%20hiring&tags=story&hitsPerPage=8')
        threads = [h for h in s.get('hits',[]) if re.search('hiring|freelancer|seeking', h.get('title','') or '', re.I)][:2]
        for thread in threads:
            item = get_json(f"https://hn.algolia.com/api/v1/items/{thread['objectID']}")
            for c in (item.get('children') or [])[:300]:
                txt = clean(c.get('text',''))
                if not txt or not match_dev(txt): continue
                push({'tipo':'freelance','fuente':'HackerNews','titulo':txt[:90]+'...','empresa':c.get('author',''),
                      'descripcion':txt[:1200],'presupuesto':'','url':f"https://news.ycombinator.com/item?id={c.get('id')}"})
    except Exception as e: print('HN:', e)

    # 8. Freelancer.com  (el endpoint de proyectos activos es PUBLICO; el token solo sube limites)
    fl_headers = {'User-Agent':UA}
    if FREELANCER_TOKEN: fl_headers['freelancer-oauth-v1'] = FREELANCER_TOKEN
    for kw in ['dashboard','wordpress','data analysis','laravel']:
        try:
            r = requests.get('https://www.freelancer.com/api/projects/0.1/projects/active/',
                params={'query':kw,'limit':12,'job_details':'true','full_description':'true'},
                headers=fl_headers, timeout=25).json()
            for p in (r.get('result',{}).get('projects') or []):
                b = p.get('budget',{}) or {}
                cur = (p.get('currency') or {}).get('code','')
                push({'tipo':'freelance','fuente':'Freelancer.com','titulo':p.get('title',''),'empresa':'',
                      'descripcion':(p.get('description','') or '')[:1200],
                      'presupuesto':f"{b.get('minimum','?')}-{b.get('maximum','?')} {cur}",
                      'url':'https://www.freelancer.com/projects/'+(p.get('seo_url','')),
                      # Datos extra para auto-aplicar (no van al CSV)
                      'fl_id':p.get('id'), 'fl_min':b.get('minimum'), 'fl_max':b.get('maximum'), 'fl_cur':cur})
        except Exception as e: print('Freelancer:', e)

    # 12. Himalayas (empleo remoto, API publica) -> mas variedad de empleo
    try:
        r = get_json('https://himalayas.app/jobs/api?limit=100')
        for j in (r.get('jobs') or []):
            blob = f"{j.get('title','')} {j.get('description','')} {' '.join(j.get('categories',[]) or [])}"
            if not match_kw(blob): continue
            push({'tipo':'empleo','fuente':'Himalayas','titulo':j.get('title',''),
                  'empresa':j.get('companyName',''),
                  'descripcion':clean(j.get('description',''))[:1200],
                  'presupuesto':(f"{j.get('minSalary')}-{j.get('maxSalary')} USD" if j.get('minSalary') else ''),
                  'url':j.get('applicationLink') or j.get('guid') or ''})
    except Exception as e: print('Himalayas:', e)

    # 9. Torre.co (LatAm)
    for kw in ['desarrollador web','analista de datos','power bi','php','laravel']:
        try:
            r = requests.post('https://search.torre.co/opportunities/_search/?size=25',
                json={'skill':{'text':kw,'experience':'potential-to-develop'}},
                headers={'Content-Type':'application/json','User-Agent':UA}, timeout=25).json()
            for o in (r.get('results') or []):
                orgs = o.get('organizations') or [{}]
                push({'tipo':'empleo','fuente':'Torre.co','titulo':o.get('objective',''),'empresa':orgs[0].get('name',''),
                      'descripcion':((o.get('objective','') or '')+' | '+', '.join(s.get('name','') for s in (o.get('skills') or [])))[:1200],
                      'presupuesto':(json.dumps(o['compensation']['data']) if o.get('compensation',{}).get('data') else ''),
                      'url':'https://torre.ai/post/'+str(o.get('id',''))})
        except Exception as e: print('Torre:', e)

    # 10. Get on Board (LatAm)
    for kw in ['php','wordpress','data','power bi','laravel','javascript']:
        try:
            r = get_json(f'https://www.getonbrd.com/api/v0/search/jobs?query={requests.utils.quote(kw)}&per_page=25&expand[]=company')
            for j in (r.get('data') or []):
                a = j.get('attributes', {}) or {}
                blob = f"{a.get('title','')} {clean(a.get('description',''))} {clean(a.get('functions',''))}"
                if not match_kw(blob): continue
                comp = (((a.get('company') or {}).get('data') or {}).get('attributes') or {}).get('name','')
                push({'tipo':'empleo','fuente':'GetOnBoard','titulo':a.get('title',''),'empresa':comp,
                      'descripcion':clean(f"{a.get('description','')} {a.get('functions','')}")[:1200],
                      'presupuesto':(f"{a.get('min_salary','?')}-{a.get('max_salary','?')} USD" if (a.get('min_salary') or a.get('max_salary')) else ''),
                      'url':'https://www.getonbrd.com/jobs/'+str(a.get('slug') or j.get('id',''))})
        except Exception as e: print('GetOnBoard:', e)

    # 11. Workana (LatAm, best-effort)
    for cat in ['it-programming','data-science-analytics','web-development']:
        try:
            htmlp = get_text(f'https://www.workana.com/jobs?category={cat}&language=es')
            seen_slug = set()
            for pr in htmlp.split('/job/')[1:120]:
                slug = pr.split('"')[0].split('?')[0].split(' ')[0].split('<')[0]
                if not slug or len(slug) < 6 or slug in seen_slug: continue
                seen_slug.add(slug)
                t = clean(slug.replace('-', ' '))
                if not match_dev(t): continue
                push({'tipo':'freelance','fuente':'Workana','titulo':t[:90],'empresa':'',
                      'descripcion':t,'presupuesto':'','url':'https://www.workana.com/job/'+slug})
        except Exception as e: print('Workana:', e)

    return out

# ============================================================
#  CALIFICADOR  (cadena de proveedores con failover por saturacion)
# ============================================================
def user_msg(j):
    return (f"TIPO: {j['tipo']}\nFUENTE: {j['fuente']}\nTITULO: {j['titulo']}\n"
            f"PRESUPUESTO: {j.get('presupuesto') or 'no dice'}\nDESCRIPCION: {j['descripcion']}")

def parse_score(txt):
    c = (txt or '').replace('```json','').replace('```','')
    m = re.search(r'\{.*\}', c, re.S)   # extrae el primer objeto JSON aunque venga con texto extra
    if m: c = m.group(0)
    p = json.loads(c)
    sc = int(float(p.get('score', 0) or 0))
    return {'score':max(0,min(10,sc)),'motivo':(p.get('motivo','') or '')[:160],'pitch':(p.get('pitch','') or '')[:500]}

def _es_rate_limit(e):
    s = str(e).lower()
    return '429' in s or 'rate limit' in s or 'quota' in s or 'resource_exhausted' in s

class RateLimit(Exception): pass

def _raw_openai(p, system, user, max_tokens=600):
    r = requests.post(p['url'],
        headers={'Content-Type':'application/json','Authorization':'Bearer '+p['key']},
        json={'model':p['model'],'temperature':0.3,'max_tokens':max_tokens,
              'response_format':{'type':'json_object'},
              'messages':[{'role':'system','content':system},{'role':'user','content':user}]},
        timeout=45)
    if r.status_code == 429: raise RateLimit('429')
    data = r.json()
    if isinstance(data, dict) and data.get('error'):
        msg = json.dumps(data['error'])
        if _es_rate_limit(msg): raise RateLimit(msg)
        raise RuntimeError(msg[:200])
    txt = (((data.get('choices') or [{}])[0]).get('message') or {}).get('content')
    if not txt: raise RuntimeError('sin texto')
    return txt

def _raw_gemini(p, system, user, max_tokens=600):
    r = requests.post(p['url'],
        headers={'Content-Type':'application/json','x-goog-api-key':p['key']},
        json={'systemInstruction':{'parts':[{'text':system}]},
              'contents':[{'role':'user','parts':[{'text':user}]}],
              'generationConfig':{'temperature':0.3,'maxOutputTokens':max_tokens,'responseMimeType':'application/json'}},
        timeout=45)
    if r.status_code == 429: raise RateLimit('429')
    data = r.json()
    if isinstance(data, dict) and data.get('error'):
        msg = json.dumps(data['error'])
        if _es_rate_limit(msg): raise RateLimit(msg)
        raise RuntimeError(msg[:200])
    cand = (data.get('candidates') or [{}])[0]
    parts = ((cand.get('content') or {}).get('parts') or [{}])
    txt = parts[0].get('text')
    if not txt: raise RuntimeError('sin texto')
    return txt

def ai_json(system, user, max_tokens=600):
    """Llama la cadena de proveedores (con failover) y devuelve el texto JSON crudo."""
    for p in PROVIDERS:
        if not p['key'] or p['name'] in AGOTADOS:
            continue
        try:
            fn = _raw_gemini if p['kind']=='gemini' else _raw_openai
            return fn(p, system, user, max_tokens), p['name']
        except RateLimit as e:
            print(f"{p['name']} saturado (se desactiva esta corrida): {str(e)[:120]}")
            AGOTADOS.add(p['name'])
        except Exception as e:
            print(f"{p['name']} fail: {str(e)[:150]}")
    return None, 'ninguno'

def calificar(j):
    txt, motor = ai_json(SYSTEM, user_msg(j))
    if motor != 'ninguno':
        try:
            s = parse_score(txt); s['motor'] = motor; return s
        except Exception as e:
            print('parse score fail:', str(e)[:120])
    return {'score':0,'motivo':'sin motor de IA disponible','pitch':'','motor':'ninguno'}

# ============================================================
#  AUTO-APLICAR A FREELANCER.COM  (opt-in, con topes)
# ============================================================
PROP_SYSTEM = (
    "Eres Frank (Francisco Quinonez), desarrollador y analista de datos colombiano con experiencia "
    "en desarrollo web (WordPress, PHP, Laravel), dashboards y analisis de datos (Power BI, SQL, Python) "
    "y automatizacion (n8n). Escribe la propuesta para una oferta de Freelancer.com, en el idioma del anuncio, "
    "en primera persona, concreta y creible, mencionando algo especifico del proyecto y como lo resolverias. "
    "Nada de relleno generico. Devuelve SOLO JSON con llaves: amount (numero, monto a cobrar dentro del "
    "presupuesto dado), period (entero, dias de entrega), proposal (string, 4-6 frases)."
)

def generar_propuesta(lead):
    pres = f"{lead.get('fl_min','?')}-{lead.get('fl_max','?')} {lead.get('fl_cur','')}"
    user = (f"TITULO: {lead['titulo']}\nPRESUPUESTO (min-max moneda): {pres}\n"
            f"DESCRIPCION: {lead['descripcion']}")
    txt, motor = ai_json(PROP_SYSTEM, user, max_tokens=700)
    if motor == 'ninguno' or not txt: return None
    try:
        c = txt.replace('```json','').replace('```','')
        m = re.search(r'\{.*\}', c, re.S)
        p = json.loads(m.group(0) if m else c)
        amount = float(p.get('amount') or lead.get('fl_min') or 0)
        period = int(p.get('period') or 7)
        proposal = (p.get('proposal') or '').strip()
        if not proposal: return None
        # Encajar el monto dentro del presupuesto si viene fuera de rango.
        lo, hi = lead.get('fl_min'), lead.get('fl_max')
        if lo and amount < lo: amount = lo
        if hi and amount > hi: amount = hi
        return {'amount':round(amount,2),'period':max(1,period),'proposal':proposal}
    except Exception as e:
        print('propuesta parse fail:', str(e)[:120]); return None

_FL_BIDDER_ID = None
def fl_bidder_id():
    global _FL_BIDDER_ID
    if _FL_BIDDER_ID: return _FL_BIDDER_ID
    if FREELANCER_USER_ID: _FL_BIDDER_ID = str(FREELANCER_USER_ID); return _FL_BIDDER_ID
    try:
        r = get_json('https://www.freelancer.com/api/users/0.1/self/',
                     headers={'freelancer-oauth-v1':FREELANCER_TOKEN})
        _FL_BIDDER_ID = str((r.get('result') or {}).get('id') or '')
    except Exception as e:
        print('fl self id fail:', str(e)[:120])
    return _FL_BIDDER_ID

def fl_enviar_bid(lead, prop):
    """Envia la oferta real a Freelancer.com. Devuelve True si quedo registrada."""
    bidder = fl_bidder_id()
    if not (bidder and lead.get('fl_id')): return False
    try:
        r = requests.post('https://www.freelancer.com/api/projects/0.1/bids/',
            headers={'freelancer-oauth-v1':FREELANCER_TOKEN,'Content-Type':'application/json','User-Agent':UA},
            json={'project_id':lead['fl_id'],'bidder_id':int(bidder),
                  'amount':prop['amount'],'period':prop['period'],
                  'milestone_percentage':50,'description':prop['proposal']},
            timeout=30).json()
        if r.get('status') == 'success' or (r.get('result') or {}).get('id'):
            return True
        print('bid rechazado:', json.dumps(r)[:200]); return False
    except Exception as e:
        print('bid fail:', str(e)[:150]); return False

def avisar_propuesta(lead, prop, enviada):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID): return
    estado = "✅ *APLICADO automaticamente*" if enviada else "📝 *Propuesta lista para revisar*"
    msg = (f"{estado}  (score {lead['score']}/10)\n\n*{lead['titulo']}*\n"
           f"💰 Oferta: {prop['amount']} {lead.get('fl_cur','')} · {prop['period']} dias\n\n"
           f"{prop['proposal']}\n\n{lead['url']}")
    try:
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id':TELEGRAM_CHAT_ID,'text':msg,'parse_mode':'Markdown','disable_web_page_preview':True},
            timeout=20)
    except Exception as e: print('Telegram propuesta:', e)

# ============================================================
#  TELEGRAM
# ============================================================
def avisar(lead):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID): return
    emp = f" • {lead['empresa']}" if lead.get('empresa') else ''
    pres = f"💰 {lead['presupuesto']}\n" if lead.get('presupuesto') else ''
    msg = (f"🟢 *Oportunidad {lead['score']}/10* ({lead['tipo']})\n\n"
           f"*{lead['titulo']}*\n📊 {lead['fuente']}{emp}\n{pres}\n"
           f"📝 _{lead['motivo']}_\n\n✍️ {lead['pitch']}\n\n{lead['url']}")
    try:
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id':TELEGRAM_CHAT_ID,'text':msg,'parse_mode':'Markdown','disable_web_page_preview':False},
            timeout=20)
    except Exception as e: print('Telegram:', e)

# ============================================================
#  MAIN
# ============================================================
def main():
    seen = load_seen()
    nuevos = recolectar(seen)
    print(f"Leads nuevos encontrados: {len(nuevos)}")

    # Seleccion ROUND-ROBIN por fuente: toma 1 de cada fuente, luego el 2o de cada una, etc.
    # Asi ninguna fuente (p.ej. Freelancer.com) inunda el cupo y entra variedad real.
    from collections import OrderedDict
    por_fuente = OrderedDict()
    for x in nuevos:
        por_fuente.setdefault(x['fuente'], []).append(x)
    por_procesar, i = [], 0
    while len(por_procesar) < MAX_POR_CORRIDA:
        avanzo = False
        for lst in por_fuente.values():
            if i < len(lst):
                por_procesar.append(lst[i]); avanzo = True
                if len(por_procesar) >= MAX_POR_CORRIDA: break
        if not avanzo: break
        i += 1
    from collections import Counter
    print(f"Encontrados por fuente: {dict(Counter(x['fuente'] for x in nuevos))}")
    print(f"Procesando {len(por_procesar)} este turno (round-robin por fuente).")

    guardados = 0
    bids_hechos = 0
    nuevas_urls = []
    filas = []
    ahora = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-5))).strftime('%Y-%m-%d %H:%M')

    for j in por_procesar:
        res = calificar(j)
        time.sleep(PAUSA_IA_S)        # respira entre llamadas (cuida limites por minuto)
        # Solo marcar como visto si la IA realmente lo evaluo.
        # Si no hubo motor (outage total / todos saturados), se deja para reintentar.
        if res['motor'] != 'ninguno':
            nuevas_urls.append(j['url'])
        if res['score'] < SCORE_GUARDAR: continue
        lead = {**j, **res, 'fecha_detectado':ahora, 'estado':'nuevo'}
        filas.append(lead)
        guardados += 1
        if res['score'] >= SCORE_AVISAR:
            avisar(lead)

        # --- Auto-aplicar a Freelancer.com (solo ofertas de esa fuente con buen score) ---
        if (FREELANCER_TOKEN and lead.get('fuente') == 'Freelancer.com'
                and lead.get('fl_id') and res['score'] >= AUTO_BID_MIN
                and bids_hechos < AUTO_BID_MAX):
            prop = generar_propuesta(lead)
            if prop:
                enviada = fl_enviar_bid(lead, prop) if AUTO_BID else False
                avisar_propuesta(lead, prop, enviada)
                if enviada: bids_hechos += 1
                time.sleep(PAUSA_IA_S)

    # Guardar CSV (append, crea cabecera si no existe)
    cols = ['fecha_detectado','tipo','fuente','titulo','empresa','presupuesto','score','motivo','pitch','motor','url','estado']
    nuevo_csv = not CSV_FILE.exists()
    with open(CSV_FILE, 'a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        if nuevo_csv: w.writeheader()
        for lead in filas: w.writerow(lead)

    # Guardar dedup
    with open(SEEN_FILE, 'a', encoding='utf-8') as f:
        for u in nuevas_urls: f.write(u+'\n')

    print(f"Guardados en CSV (score>={SCORE_GUARDAR}): {guardados}")
    if FREELANCER_TOKEN:
        modo = 'aplicadas' if AUTO_BID else 'preparadas para revisar'
        print(f"Propuestas Freelancer {modo} esta corrida: {bids_hechos if AUTO_BID else 'segun Telegram'}")

if __name__ == '__main__':
    main()
