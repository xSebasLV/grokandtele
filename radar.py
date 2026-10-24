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

SCORE_GUARDAR  = 6   # se guarda en el CSV si score >= esto
SCORE_AVISAR   = 8   # se avisa por Telegram si score >= esto

# Modelos (todos gratis, formato OpenAI). Cambia si los nombres rotan.
GROQ_MODEL = 'openai/gpt-oss-120b'   # gratis en Groq, mejor calidad. Alt mas rapido: 'openai/gpt-oss-20b'
NIM_MODEL  = 'meta/llama-3.3-70b-instruct'
GLM_MODEL  = 'glm-4.7-flash'

# Keys desde entorno
GROQ_API_KEY     = os.environ.get('GROQ_API_KEY', '')
NVIDIA_API_KEY   = os.environ.get('NVIDIA_API_KEY', '')
GLM_API_KEY      = os.environ.get('GLM_API_KEY', '')
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
FREELANCER_TOKEN = os.environ.get('FREELANCER_TOKEN', '')

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

DATA_DIR  = pathlib.Path(__file__).parent / 'data'
SEEN_FILE = DATA_DIR / 'seen.txt'
CSV_FILE  = DATA_DIR / 'leads.csv'
DATA_DIR.mkdir(exist_ok=True)

SYSTEM = (
    "Eres el filtro de oportunidades de Impulsate / PCMarkett (Bogota, Colombia). "
    "El usuario busca tanto proyectos freelance como un posible segundo empleo remoto. "
    "Perfil/servicios: desarrollo web (WordPress, Elementor, PHP, Laravel), sistemas de "
    "informacion a medida (inventarios, CRM, ERP), analisis de datos y dashboards (Power BI, "
    "DAX, SQL, MySQL), automatizacion (n8n), SEO y marketing digital. NO: apps moviles nativas, "
    "blockchain, ML pesado, 3D, redaccion pura, diseno grafico puro. Devuelve SOLO un objeto JSON "
    "valido (sin markdown) con llaves: score (entero 0-10 de match real con el perfil), "
    "motivo (string max 20 palabras en espanol), pitch (2 frases en el idioma del anuncio, "
    "apertura de propuesta o mensaje personalizado que mencione algo concreto del anuncio)."
)

# ============================================================
#  HELPERS
# ============================================================
def clean(s):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', ' ', s or '')).strip()

def match_kw(txt):
    t = (txt or '').lower()
    return any(k in t for k in KEYWORDS)

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
    for sub in ['forhire','hiring','jobbit']:
        try:
            r = get_json(f'https://www.reddit.com/r/{sub}/new.json?limit=100')
            for p in r.get('data',{}).get('children', []):
                d = p.get('data', {})
                if '[hiring]' not in (d.get('title','').lower()): continue
                if not match_kw(f"{d.get('title','')} {d.get('selftext','')}"): continue
                push({'tipo':'freelance','fuente':f'Reddit r/{sub}','titulo':d.get('title',''),'empresa':d.get('author',''),
                      'descripcion':(d.get('selftext','') or '')[:1200],'presupuesto':'',
                      'url':'https://www.reddit.com'+d.get('permalink','')})
        except Exception as e: print('Reddit', sub, e)

    # 7. HackerNews (Who is hiring / Freelancer seeking)
    try:
        s = get_json('https://hn.algolia.com/api/v1/search?query=Ask%20HN%20hiring%20freelancer&tags=story&hitsPerPage=5')
        thread = next((h for h in s.get('hits',[]) if re.search('hiring|freelancer', h.get('title','') or '', re.I)), None)
        if thread:
            item = get_json(f"https://hn.algolia.com/api/v1/items/{thread['objectID']}")
            for c in (item.get('children') or [])[:300]:
                txt = clean(c.get('text',''))
                if not txt or not match_kw(txt): continue
                push({'tipo':'freelance','fuente':'HackerNews','titulo':txt[:90]+'...','empresa':c.get('author',''),
                      'descripcion':txt[:1200],'presupuesto':'','url':f"https://news.ycombinator.com/item?id={c.get('id')}"})
    except Exception as e: print('HN:', e)

    # 8. Freelancer.com (opcional)
    if FREELANCER_TOKEN:
        for kw in ['dashboard','power bi','php','wordpress','data analysis','laravel']:
            try:
                r = requests.get('https://www.freelancer.com/api/projects/0.1/projects/active/',
                    params={'query':kw,'limit':25,'job_details':'true','full_description':'true'},
                    headers={'freelancer-oauth-v1':FREELANCER_TOKEN,'User-Agent':UA}, timeout=25).json()
                for p in (r.get('result',{}).get('projects') or []):
                    b = p.get('budget',{}) or {}
                    push({'tipo':'freelance','fuente':'Freelancer.com','titulo':p.get('title',''),'empresa':'',
                          'descripcion':(p.get('description','') or '')[:1200],
                          'presupuesto':f"{b.get('minimum','?')}-{b.get('maximum','?')} {(p.get('currency') or {}).get('code','')}",
                          'url':'https://www.freelancer.com/projects/'+(p.get('seo_url',''))})
            except Exception as e: print('Freelancer:', e)

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
                if not match_kw(t): continue
                push({'tipo':'freelance','fuente':'Workana','titulo':t[:90],'empresa':'',
                      'descripcion':t,'presupuesto':'','url':'https://www.workana.com/job/'+slug})
        except Exception as e: print('Workana:', e)

    return out

# ============================================================
#  CALIFICADOR  (Groq -> NIM -> GLM)
# ============================================================
def user_msg(j):
    return (f"TIPO: {j['tipo']}\nFUENTE: {j['fuente']}\nTITULO: {j['titulo']}\n"
            f"PRESUPUESTO: {j.get('presupuesto') or 'no dice'}\nDESCRIPCION: {j['descripcion']}")

def parse_score(txt):
    c = (txt or '').replace('```json','').replace('```','').strip()
    p = json.loads(c)
    return {'score':int(p.get('score',0) or 0),'motivo':p.get('motivo','') or '','pitch':p.get('pitch','') or ''}

def _chat(url, key, model, j):
    r = requests.post(url, headers={'Content-Type':'application/json','Authorization':'Bearer '+key},
        json={'model':model,'temperature':0.2,'max_tokens':400,
              'messages':[{'role':'system','content':SYSTEM},{'role':'user','content':user_msg(j)}]},
        timeout=40).json()
    txt = (((r.get('choices') or [{}])[0]).get('message') or {}).get('content')
    if not txt: raise RuntimeError('sin texto: '+json.dumps(r)[:200])
    return parse_score(txt)

def calificar(j):
    # 1a opcion: Groq
    if GROQ_API_KEY:
        try:
            s = _chat('https://api.groq.com/openai/v1/chat/completions', GROQ_API_KEY, GROQ_MODEL, j)
            s['motor']='groq'; return s
        except Exception as e: print('Groq fail:', e)
    # 2a opcion: NVIDIA NIM
    if NVIDIA_API_KEY:
        try:
            s = _chat('https://integrate.api.nvidia.com/v1/chat/completions', NVIDIA_API_KEY, NIM_MODEL, j)
            s['motor']='nim'; return s
        except Exception as e: print('NIM fail:', e)
    # 3a opcion: GLM (z.ai)
    if GLM_API_KEY:
        try:
            s = _chat('https://api.z.ai/api/paas/v4/chat/completions', GLM_API_KEY, GLM_MODEL, j)
            s['motor']='glm'; return s
        except Exception as e: print('GLM fail:', e)
    return {'score':0,'motivo':'sin motor de IA disponible','pitch':'','motor':'ninguno'}

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

    guardados = 0
    nuevas_urls = []
    filas = []
    ahora = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-5))).strftime('%Y-%m-%d %H:%M')

    for j in nuevos:
        res = calificar(j)
        time.sleep(0.4)               # respira entre llamadas
        # Solo marcar como visto si la IA realmente lo evaluo.
        # Si no hubo motor (outage total), se deja para reintentar en la proxima corrida.
        if res['motor'] != 'ninguno':
            nuevas_urls.append(j['url'])
        if res['score'] < SCORE_GUARDAR: continue
        lead = {**j, **res, 'fecha_detectado':ahora, 'estado':'nuevo'}
        filas.append(lead)
        guardados += 1
        if res['score'] >= SCORE_AVISAR:
            avisar(lead)

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

if __name__ == '__main__':
    main()
