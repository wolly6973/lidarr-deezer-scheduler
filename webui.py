"""Authenticated web UI for the Lidarr Deezer Scheduler."""
import base64, json, logging, os, secrets, sqlite3, sys, threading, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

DATA=Path(os.getenv("DB_PATH","/data/scheduler.db")).parent
SETTINGS_FILE=DATA/"webui-settings.json"; PASSWORD_FILE=DATA/"webui_password"; LOG_FILE=DATA/"scheduler.log"
FIELDS={
"LIDARR_URL":("Lidarr URL","str",1,500),"LIDARR_API_KEY":("Lidarr API key (leave blank to keep current key)","secret",0,500),
"TZ":("Time zone (IANA name)","str",1,100),"LOG_LEVEL":("Log level","choice",["DEBUG","INFO","WARNING","ERROR","CRITICAL"],None),
"WEBUI_USERNAME":("Web UI username","str",1,100),
"BATCH_SIZE":("Albums per search batch","int",1,100),"MIN_SEARCHES_PER_DAY":("Minimum searches per day","int",1,100000),
"MAX_SEARCHES_PER_DAY":("Maximum searches per day","int",1,100000),"MIN_DOWNLOADS_PER_DAY":("Minimum grabs per day","int",0,100000),
"MAX_DOWNLOADS_PER_DAY":("Maximum grabs per day (0 = unlimited)","int",0,100000),
"MIN_CHUNK_DELAY_SECONDS":("Minimum batch delay (seconds)","int",60,604800),"MAX_CHUNK_DELAY_SECONDS":("Maximum batch delay (seconds)","int",60,604800),
"IDLE_RETRY_SECONDS":("Idle retry delay (seconds)","int",60,86400),"ALBUM_COOLDOWN_HOURS":("Album cooldown (hours)","float",0,87600),
"COMMAND_TIMEOUT_SECONDS":("Command timeout (seconds)","int",60,86400),"SEARCH_ORDER":("Ordering within fetched page","choice",["newest_artist","random"],None),
"MISSING_PAGE_SIZE":("Missing albums per refresh page","int",50,5000),"MISSING_REFRESH_MINUTES":("Minimum page age before rotation (minutes)","int",5,10080),
"MANAGE_DEEZER_AUTOMATIC_SEARCH":("Manage Deezer Automatic Search","bool",None,None),"DEEZER_INDEXER_NAME":("Deezer indexer name","str",1,200),
"MANAGE_OTHER_INDEXERS":("Temporarily disable other indexers","bool",None,None),"OTHER_INDEXERS_EXCLUDE":("Excluded indexers (comma-separated)","str",0,2000),
"SAFETY_CHECK_ENABLED":("Enable safety checks","bool",None,None),"DEEZER_DOWNLOAD_CLIENT_NAME":("Deezer download client (optional)","str",0,200),
"FAIL_LOOKBACK_HOURS":("Failure lookback (hours)","float",1,720),"FAIL_MIN_FAILURES":("Minimum failures to pause","int",1,10000),
"FAIL_RATIO":("Failure ratio (0–1)","float",0,1),"FAIL_PAUSE_HOURS":("Safety pause duration (hours)","float",0.5,720)}
AUTH_USERNAME=os.getenv("WEBUI_USERNAME","admin")
BOOLS={k for k,v in FIELDS.items() if v[1]=="bool"}
def mod(): return sys.modules.get("scheduler") or sys.modules.get("__main__")
def current(include_secrets=False):
 d={}
 for k,(label,kind,lo,hi) in FIELDS.items():
  if k=="LIDARR_API_KEY":
   d[k]=getattr(mod(),"LIDARR_API_KEY","") if include_secrets else ""
  elif k=="WEBUI_USERNAME":
   d[k]=AUTH_USERNAME
  else:
   v=getattr(mod(),k)
   d[k]=", ".join(sorted(v)) if k=="OTHER_INDEXERS_EXCLUDE" else v
 return d

def apply(values,persist=True):
 global AUTH_USERNAME
 if not isinstance(values,dict): raise ValueError("Settings must be an object")
 unknown=set(values)-set(FIELDS)
 if unknown: raise ValueError("Unknown settings: "+", ".join(sorted(unknown)))
 clean=current(True)
 for k,raw in values.items():
  label,kind,lo,hi=FIELDS[k]
  if kind=="bool":
   if not isinstance(raw,bool): raise ValueError(label+" must be true or false")
   v=raw
  elif kind=="choice":
   if raw not in lo: raise ValueError(label+" has an invalid choice")
   v=raw
  elif kind=="secret":
   if not isinstance(raw,str): raise ValueError(label+" must be text")
   v=raw.strip() or getattr(mod(),"LIDARR_API_KEY","")
   if not v: raise ValueError("Enter your Lidarr API key before saving settings")
   if len(v)>hi: raise ValueError(label+" is too long")
  elif kind=="str":
   if not isinstance(raw,str): raise ValueError(label+" must be text")
   v=raw.strip()
   if lo and len(v)<lo: raise ValueError(label+" cannot be empty")
   if len(v)>hi: raise ValueError(label+" is too long")
  else:
   try: v=float(raw) if kind=="float" else int(raw)
   except (ValueError,TypeError): raise ValueError(label+" must be numeric")
   if v<lo or v>hi: raise ValueError(f"{label} must be between {lo} and {hi}")
   if kind=="int": v=int(v)
  clean[k]=v
 if clean["MIN_SEARCHES_PER_DAY"]>clean["MAX_SEARCHES_PER_DAY"]: raise ValueError("Minimum searches cannot exceed maximum")
 if clean["MAX_DOWNLOADS_PER_DAY"]>0 and clean["MIN_DOWNLOADS_PER_DAY"]>clean["MAX_DOWNLOADS_PER_DAY"]: raise ValueError("Minimum grabs cannot exceed maximum")
 if clean["MIN_CHUNK_DELAY_SECONDS"]>clean["MAX_CHUNK_DELAY_SECONDS"]: raise ValueError("Minimum delay cannot exceed maximum")
 parsed=urlparse(clean["LIDARR_URL"])
 if parsed.scheme not in ("http","https") or not parsed.netloc: raise ValueError("Lidarr URL must start with http:// or https:// and include a host")
 try: ZoneInfo(clean["TZ"])
 except Exception: raise ValueError("Use a valid IANA time zone, such as America/Chicago")
 for k,v in clean.items():
  if k=="LIDARR_API_KEY": setattr(mod(),k,v)
  elif k=="WEBUI_USERNAME": AUTH_USERNAME=v
  else: setattr(mod(),k,{n.strip().lower() for n in v.split(",") if n.strip()} if k=="OTHER_INDEXERS_EXCLUDE" else v)
 mod().LIDARR_URL=clean["LIDARR_URL"].rstrip("/")
 mod().LOG_LEVEL=clean["LOG_LEVEL"]
 mod().log.setLevel(getattr(logging,clean["LOG_LEVEL"]))
 logging.getLogger().setLevel(getattr(logging,clean["LOG_LEVEL"]))
 os.environ["TZ"]=clean["TZ"]
 if hasattr(time,"tzset"): time.tzset()
 if hasattr(apply,"scheduler") and apply.scheduler is not None:
  apply.scheduler.session.headers.update({"X-Api-Key":clean["LIDARR_API_KEY"]})
 if persist:
  DATA.mkdir(parents=True,exist_ok=True); SETTINGS_FILE.write_text(json.dumps(clean,indent=2)+"\n"); SETTINGS_FILE.chmod(0o600)
 logging.getLogger(__name__).info("Web UI saved %d settings",len(values))
 return current()
apply.scheduler=None

HTML=r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Lidarr Deezer Scheduler</title><style>
:root{color-scheme:dark;--bg:#17191d;--panel:#22252b;--line:#3b414b;--text:#e8eaf0;--muted:#a4adba;--accent:#f28c28;--green:#56bd82;--red:#ed7777;--yellow:#e9c45d}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,Segoe UI,sans-serif}.top{background:#101114;border-bottom:3px solid var(--accent);padding:15px 22px;font-size:18px;font-weight:750}.top b{color:var(--accent)}.layout{display:grid;grid-template-columns:200px 1fr;min-height:calc(100vh - 55px)}aside{background:#1d2025;border-right:1px solid var(--line);padding:14px}nav button{display:block;width:100%;text-align:left;margin:4px 0;background:transparent;border:0;padding:11px;color:var(--muted)}nav button.active,nav button:hover{background:#34302a;color:white;border-left:3px solid var(--accent)}main{padding:24px;max-width:1500px;width:100%;margin:auto}.head{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:18px}.head h1{font-size:24px;font-weight:550;margin:0}.muted{color:var(--muted)}.spacer{flex:1}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.card{background:var(--panel);border:1px solid var(--line);border-radius:5px;padding:16px;margin-bottom:14px;min-width:0}.stat small{display:block;color:var(--muted)}.stat strong{display:block;font-size:27px;margin:5px 0}.btn{padding:8px 12px;border-radius:4px;border:1px solid var(--line);background:#30343b;color:var(--text);cursor:pointer}.primary{background:var(--accent);color:#191919;border-color:var(--accent);font-weight:700}.danger{color:#ffaaaa;border-color:#8d4545}.hidden{display:none!important}.groups{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.field{display:grid;grid-template-columns:1fr minmax(130px,42%);gap:12px;align-items:center;padding:11px 0;border-bottom:1px solid #343941}.field:last-child{border:0}.field small{display:block;color:var(--muted);font-size:11px}.field input,.field select{width:100%;padding:8px;background:#17191d;border:1px solid #454b55;border-radius:4px;color:var(--text)}.field input[type=checkbox]{width:18px;accent-color:var(--accent)}.logpane{height:60vh;overflow:auto;background:#101114;border:1px solid var(--line);padding:12px;font:12px/1.7 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere}.INFO{color:#bdc7d5}.WARNING{color:var(--yellow)}.ERROR,.CRITICAL{color:#ff8585}.DEBUG{color:#8db2df}.notice{padding:12px;background:#302a1c;border:1px solid #67552a;margin-bottom:14px}.ok{color:var(--green)}.bad{color:var(--red)}.actions{display:flex;gap:8px;flex-wrap:wrap}@media(max-width:850px){.layout{grid-template-columns:1fr}nav{display:flex;overflow:auto}nav button{white-space:nowrap;width:auto}.groups{grid-template-columns:1fr}main{padding:14px}}@media(max-width:520px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.field{grid-template-columns:1fr}.top{padding:12px}}</style></head><body>
<header class="top"><b>♪</b> Lidarr Deezer Scheduler <span class="muted">/ Web UI</span></header><div class="layout"><aside><nav><button class="active" data-tab="dashboard">▦ &nbsp; Dashboard</button><button data-tab="settings">⚙ &nbsp; Settings</button><button data-tab="logs">≡ &nbsp; Logs</button><button data-tab="safety">⛨ &nbsp; Safety & Recovery</button><button data-tab="about">ⓘ &nbsp; About</button></nav></aside><main>
<section id="dashboard"><div class="head"><h1>Dashboard</h1><span class="spacer"></span><button class="btn" onclick="refresh()">↻ Refresh</button></div><div id="banner"></div><div class="grid" id="stats"></div><div class="card"><h3>Scheduler overview</h3><div id="overview">Loading…</div></div><div class="card"><h3>Recent activity</h3><div id="recent"></div><button class="btn" onclick="tab('logs')">View all logs →</button></div></section>
<section id="settings" class="hidden"><div class="head"><h1>Settings</h1><span class="spacer"></span><button class="btn" onclick="loadSettings()">Reload</button><button class="btn primary" onclick="save()">Save changes</button></div><div class="notice">Settings apply immediately and persist in <code>/data/webui-settings.json</code>. Missing albums are fetched one page at a time and retained until exhausted to reduce Lidarr load. Keep safety checks enabled.</div><div class="groups" id="fields"></div><button class="btn primary" onclick="save()">Save changes</button></section>
<section id="logs" class="hidden"><div class="head"><h1>Logs</h1></div><div class="actions" style="margin-bottom:12px"><select id="level" class="btn"><option>ALL</option><option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option><option>CRITICAL</option></select><input id="query" class="btn" placeholder="Filter logs…" oninput="loadLogs()" /><button class="btn" onclick="loadLogs()">Refresh</button></div><div class="logpane" id="logpane"></div></section>
<section id="safety" class="hidden"><div class="head"><h1>Safety & Recovery</h1></div><div class="card" id="safestatus">Loading…</div><div class="card"><h3>Reset a safety pause</h3><p>This clears the active pause and ignores failures recorded before the reset time. It does <b>not</b> disable safety checks or clear history, cooldowns, or daily caps.</p><label><input id="confirm" type="checkbox"> I fixed the underlying issue and want to clear the pause.</label><p><button class="btn danger" id="reset" disabled onclick="resetPause()">Reset safety pause</button></p></div></section>
<section id="about" class="hidden"><div class="head"><h1>About</h1></div><div class="card"><p>This UI manages the existing Lidarr Deezer Scheduler process. It does not talk to Deezer directly.</p><p>All routes require HTTP Basic Authentication. The generated password is stored at <code>/data/webui_password</code>.</p><p class="muted">For remote access, use a trusted network and HTTPS through a reverse proxy. Do not expose this HTTP port directly to the internet.</p></div></section>
</main></div><div id="toast" style="position:fixed;right:18px;bottom:18px;background:#31563c;padding:12px 16px;display:none;border-radius:4px"></div>
<script>
let settings={},labels={},types={},status={},keyConfigured=false;const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,opts={}){let r=await fetch(url,{...opts,headers:{'Content-Type':'application/json',...(opts.headers||{})}});let d=await r.json();if(!r.ok)throw Error(d.error||r.statusText);return d}
function toast(s,bad=false){$('toast').textContent=s;$('toast').style.display='block';$('toast').style.background=bad?'#672e2e':'#31563c';setTimeout(()=>$('toast').style.display='none',3500)}
function tab(t){document.querySelectorAll('main section').forEach(s=>s.classList.toggle('hidden',s.id!==t));document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab===t));if(t==='logs')loadLogs();if(t==='safety')renderSafety()}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
function fmt(v){return v?new Date(Number(v)*1000).toLocaleString():'—'}function dur(n){n=Math.max(0,Number(n)||0);return Math.floor(n/3600)+'h '+Math.floor(n%3600/60)+'m'}
async function refresh(){try{status=await api('/api/status');let p=status.state==='paused';$('banner').innerHTML=p?'<div class="notice">⚠ Safety pause active. Check the logs and resolve the cause before resetting.</div>':'';$('stats').innerHTML=[['Scheduler',p?'PAUSED':'RUNNING',p?'Safety pause':'Normal operation'],['Searches today',status.searches_today,status.search_cap==null?'Daily cap unavailable':'Cap '+status.search_cap],['Deezer grabs today',status.grabs_today??'—',status.grab_cap==null?'No cap / unavailable':'Cap '+status.grab_cap],['Missing albums',Number(status.missing_total||0).toLocaleString(),'Lidarr wanted list']].map(x=>'<div class="card stat"><small>'+x[0]+'</small><strong class="'+(p?'bad':'')+'">'+x[1]+'</strong><small>'+x[2]+'</small></div>').join('');$('overview').innerHTML='<p><b>Last search:</b> '+fmt(status.last_search)+'</p><p><b>Safety checks:</b> '+(status.safety_enabled?'Enabled':'DISABLED')+'</p><p><b>Tracked albums:</b> '+Number(status.tracked_albums||0).toLocaleString()+'</p><p><b>Search events:</b> '+Number(status.total_search_events||0).toLocaleString()+'</p>';$('recent').innerHTML='';await loadLogs(true);if(!$('safety').classList.contains('hidden'))renderSafety()}catch(e){$('overview').textContent='API error: '+e.message;toast(e.message,true)}}
async function loadSettings(){try{let d=await api('/api/settings');settings=d.settings;labels=d.labels;types=d.types;keyConfigured=d.key_configured;let groups={'Lidarr connection':['LIDARR_URL','LIDARR_API_KEY'],'General':['TZ','LOG_LEVEL','WEBUI_USERNAME'],'Search limits':['BATCH_SIZE','MIN_SEARCHES_PER_DAY','MAX_SEARCHES_PER_DAY','MIN_DOWNLOADS_PER_DAY','MAX_DOWNLOADS_PER_DAY'],'Timing':['MIN_CHUNK_DELAY_SECONDS','MAX_CHUNK_DELAY_SECONDS','IDLE_RETRY_SECONDS','ALBUM_COOLDOWN_HOURS','COMMAND_TIMEOUT_SECONDS'],'Missing albums':['SEARCH_ORDER','MISSING_PAGE_SIZE','MISSING_REFRESH_MINUTES'],'Indexer management':['MANAGE_DEEZER_AUTOMATIC_SEARCH','DEEZER_INDEXER_NAME','MANAGE_OTHER_INDEXERS','OTHER_INDEXERS_EXCLUDE'],'Safety checks':['SAFETY_CHECK_ENABLED','DEEZER_DOWNLOAD_CLIENT_NAME','FAIL_LOOKBACK_HOURS','FAIL_MIN_FAILURES','FAIL_RATIO','FAIL_PAUSE_HOURS']};$('fields').innerHTML=Object.entries(groups).map(([g,ks])=>'<div class="card"><h3>'+g+'</h3>'+ks.map(k=>'<div class="field"><label>'+esc(labels[k]||k)+'</label>'+input(k)+'</div>').join('')+(g==='Lidarr connection'?'<button class="btn" onclick="testConnection()">Test Lidarr connection</button>':'')+'</div>').join('')}catch(e){toast(e.message,true)}}
function input(k){let t=types[k],v=settings[k],id='f-'+k;if(t==='bool')return '<input id="'+id+'" type="checkbox" '+(v?'checked':'')+'>';if(t==='secret')return '<input id="'+id+'" type="password" autocomplete="new-password" placeholder="'+(keyConfigured?'Leave blank to keep current key':'Enter Lidarr API key')+'">';if(t==='choice')return '<select id="'+id+'">'+(k==='SEARCH_ORDER'?['newest_artist','random']:['DEBUG','INFO','WARNING','ERROR','CRITICAL']).map(x=>'<option '+(v===x?'selected':'')+' value="'+x+'">'+(x==='random'?'Random':x==='newest_artist'?'Newest artist first (within page)':x)+'</option>').join('')+'</select>';return '<input id="'+id+'" type="'+(t==='str'?'text':'number')+'" '+(t==='float'?'step="any"':'')+' value="'+esc(v)+'">'}
async function testConnection(){try{let d=await api('/api/connection');toast(d.message)}catch(e){toast(e.message,true)}}
async function save(){try{let out={};for(let k of Object.keys(settings)){let e=$('f-'+k);if(!e)continue;out[k]=types[k]==='bool'?e.checked:types[k]==='int'?Number(e.value):types[k]==='float'?Number(e.value):e.value}let d=await api('/api/settings',{method:'POST',body:JSON.stringify({settings:out})});settings=d.settings;keyConfigured=true;toast('Settings saved and applied.');await refresh()}catch(e){toast(e.message,true)}}
async function loadLogs(recent=false){try{let q=new URLSearchParams({level:recent?'ALL':$('level').value,q:recent?'':$('query').value,limit:recent?'8':'500'});let d=await api('/api/logs?'+q);let html=d.logs.map(x=>'<div class="'+esc(x.level)+'">'+esc(x.time)+' | '+esc(x.level)+' | '+esc(x.message)+'</div>').join('')||'<span class="muted">No matching log entries.</span>';if(recent)$('recent').innerHTML=html;else $('logpane').innerHTML=html}catch(e){toast(e.message,true)}}
function renderSafety(){if(!status.state)return;let p=status.state==='paused';$('safestatus').innerHTML='<h3 class="'+(p?'bad':'ok')+'">'+(p?'Safety pause active':'No active safety pause')+'</h3><p>'+(p?'Pause expires '+fmt(status.paused_until)+' (in '+dur(status.pause_remaining)+').':'Scheduler is not currently paused.')+'</p><p>Safety checks: <b>'+(status.safety_enabled?'Enabled':'Disabled')+'</b></p>'}
$('confirm').onchange=()=>$('reset').disabled=!$('confirm').checked;
async function resetPause(){if(!$('confirm').checked)return;try{let d=await api('/api/safety/reset',{method:'POST',body:JSON.stringify({confirm:true})});toast(d.message);$('confirm').checked=false;await refresh()}catch(e){toast(e.message,true)}finally{$('reset').disabled=!$('confirm').checked}}
$('level').onchange=()=>loadLogs();refresh();loadSettings();setInterval(()=>{if(!$('dashboard').classList.contains('hidden'))refresh();if(!$('logs').classList.contains('hidden'))loadLogs()},15000);
</script></body></html>'''
class Ring(logging.Handler):
 def __init__(self): super().__init__(); self.rows=[]; self.lock=threading.RLock()
 def emit(self,r):
  row={"time":datetime.fromtimestamp(r.created).astimezone().isoformat(timespec="seconds"),"level":r.levelname,"message":self.format(r)}
  with self.lock: self.rows=(self.rows+[row])[-1200:]
 def read(self,limit=500,level="ALL",q=""):
  with self.lock: rows=list(self.rows)
  rows=[x for x in rows if (level=="ALL" or x["level"]==level) and q.lower() in x["message"].lower()]
  return rows[-max(1,min(int(limit),1000)):]
ring=Ring(); ring.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
_grab_cache={"at":0.0,"value":None}; _grab_cache_lock=threading.Lock()
def status(s):
 now=int(time.time()); db=sqlite3.connect(os.getenv("DB_PATH","/data/scheduler.db"),timeout=10); db.row_factory=sqlite3.Row
 try:
  def kv(k):
   r=db.execute("SELECT value FROM kv WHERE key=?",(k,)).fetchone(); return r["value"] if r else None
  paused=int(kv("paused_until") or 0); midnight=datetime.now().astimezone().replace(hour=0,minute=0,second=0,microsecond=0).timestamp(); today=datetime.now().astimezone().strftime("%Y-%m-%d")
  searches=db.execute("SELECT COUNT(*) FROM search_events WHERE searched_at>=?",(midnight,)).fetchone()[0]
  tracked=db.execute("SELECT COUNT(*) FROM album_state").fetchone()[0]; events=db.execute("SELECT COUNT(*) FROM search_events").fetchone()[0]; last=db.execute("SELECT MAX(searched_at) FROM search_events").fetchone()[0]
  sc=int(kv("search_cap_value")) if kv("search_cap_day")==today and kv("search_cap_value") else None
  gc=int(kv("grab_cap_value")) if kv("grab_cap_day")==today and kv("grab_cap_value") else None
 finally: db.close()
 grabs=None
 if getattr(mod(),"MAX_DOWNLOADS_PER_DAY",30)>0:
  with _grab_cache_lock:
   if time.time()-_grab_cache["at"]>=30:
    try: _grab_cache["value"]=s.deezer_grabs_today(); _grab_cache["at"]=time.time()
    except Exception: logging.getLogger(__name__).debug("Could not refresh today's Deezer grab count",exc_info=True)
   grabs=_grab_cache["value"]
 return {"state":"paused" if paused>now else "running","paused_until":paused if paused>now else 0,"pause_remaining":max(0,paused-now),"searches_today":searches,"search_cap":sc,"grabs_today":grabs,"grab_cap":gc,"tracked_albums":tracked,"total_search_events":events,"last_search":last,"missing_total":getattr(s,"missing_total",0),"safety_enabled":getattr(mod(),"SAFETY_CHECK_ENABLED",True),"lidarr_url":getattr(mod(),"LIDARR_URL","")}
def start_webui(scheduler):
 DATA.mkdir(parents=True,exist_ok=True); root=logging.getLogger(); root.addHandler(ring)
 fh=logging.FileHandler(LOG_FILE,encoding="utf-8"); fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")); root.addHandler(fh)
 if not PASSWORD_FILE.exists(): PASSWORD_FILE.write_text(secrets.token_urlsafe(18)+"\n"); PASSWORD_FILE.chmod(0o600); logging.warning("Web UI password created at %s",PASSWORD_FILE)
 saved={}
 try: saved=json.loads(SETTINGS_FILE.read_text())
 except (OSError,ValueError): pass
 if saved:
  try: apply(saved,True)
  except Exception: logging.exception("Saved Web UI settings invalid; using environment defaults")
 else:
  try: apply(current(True),True)
  except Exception: logging.info("Lidarr connection is not configured yet; configure it in the Web UI")
 apply.scheduler=scheduler
 if getattr(scheduler,"session",None) is not None:
  scheduler.session.headers.update({"X-Api-Key":getattr(mod(),"LIDARR_API_KEY","")})
 password=PASSWORD_FILE.read_text().strip()
 class Handler(BaseHTTPRequestHandler):
  server_version="DeezerSchedulerUI/1.0"
  def log_message(self,fmt,*args): logging.getLogger(__name__).debug(fmt,*args)
  def auth(self):
   expected="Basic "+base64.b64encode((AUTH_USERNAME+":"+password).encode()).decode()
   if secrets.compare_digest(self.headers.get("Authorization",""),expected): return True
   self.send_response(401);self.send_header("WWW-Authenticate",'Basic realm="Lidarr Deezer Scheduler"');self.send_header("Content-Length","0");self.end_headers();return False
  def send(self,code,data,ctype="application/json; charset=utf-8"):
   raw=data.encode() if isinstance(data,str) else json.dumps(data).encode()
   self.send_response(code);self.send_header("Content-Type",ctype);self.send_header("Content-Length",str(len(raw)));self.send_header("Cache-Control","no-store");self.send_header("X-Content-Type-Options","nosniff");self.send_header("Content-Security-Policy","default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'");self.end_headers();self.wfile.write(raw)
  def body(self):
   n=int(self.headers.get("Content-Length","0"))
   if n>65536: raise ValueError("Request body too large")
   return json.loads(self.rfile.read(n) or b"{}")
  def do_GET(self):
   if not self.auth(): return
   u=urlparse(self.path)
   if u.path=="/": return self.send(200,HTML,"text/html; charset=utf-8")
   if u.path=="/api/status":
    try:return self.send(200,status(scheduler))
    except Exception:logging.exception("Status failed");return self.send(500,{"error":"Could not load scheduler status"})
   if u.path=="/api/settings": return self.send(200,{"settings":current(),"labels":{k:v[0] for k,v in FIELDS.items()},"types":{k:v[1] for k,v in FIELDS.items()},"key_configured":bool(getattr(mod(),"LIDARR_API_KEY",""))})
   if u.path=="/api/logs":
    q=parse_qs(u.query);level=q.get("level",["ALL"])[0].upper();needle=q.get("q",[""])[0];limit=q.get("limit",["300"])[0]
    if level not in {"ALL","DEBUG","INFO","WARNING","ERROR","CRITICAL"}: return self.send(400,{"error":"Invalid log level"})
    rows=[]
    try:
     for line in LOG_FILE.read_text(errors="replace").splitlines()[-1500:]:
      p=line.split(" | ",2)
      if len(p)==3: rows.append({"time":p[0],"level":p[1],"message":line})
    except OSError: pass
    rows+=ring.read(1000); seen=set(); out=[]
    for x in rows:
     key=(x["time"],x["message"])
     if key not in seen: seen.add(key);out.append(x)
    out=[x for x in out if (level=="ALL" or x["level"]==level) and needle.lower() in x["message"].lower()]
    return self.send(200,{"logs":out[-max(1,min(int(limit),1000)):]})
   if u.path=="/api/connection":
    try:scheduler.get_json("system/status");return self.send(200,{"ok":True,"message":"Lidarr API reachable"})
    except Exception as e:return self.send(502,{"ok":False,"message":str(e)[:240]})
   return self.send(404,{"error":"Not found"})
  def do_POST(self):
   if not self.auth():return
   try: body=self.body()
   except Exception as e:return self.send(400,{"error":str(e)})
   u=urlparse(self.path)
   if u.path=="/api/settings":
    try: apply(body.get("settings",{})); return self.send(200,{"ok":True,"settings":current()})
    except ValueError as e:return self.send(400,{"error":str(e)})
    except Exception:logging.exception("Settings update failed");return self.send(500,{"error":"Unable to save settings"})
   if u.path=="/api/safety/reset":
    if not isinstance(body,dict) or body.get("confirm") is not True:return self.send(400,{"error":"Confirmation required"})
    now=int(time.time())
    try:
     with sqlite3.connect(os.getenv("DB_PATH","/data/scheduler.db"),timeout=10) as db:
      db.execute("INSERT INTO kv(key,value) VALUES('paused_until','0') ON CONFLICT(key) DO UPDATE SET value='0'")
      db.execute("INSERT INTO kv(key,value) VALUES('failure_ignore_before',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(now),))
     logging.warning("Safety pause reset in Web UI; prior failures ignored before %d",now)
     return self.send(200,{"ok":True,"message":"Safety pause cleared. Earlier failures are ignored; safety checks remain enabled."})
    except Exception:logging.exception("Pause reset failed");return self.send(500,{"error":"Could not reset safety pause"})
   return self.send(404,{"error":"Not found"})
 server=ThreadingHTTPServer((os.getenv("WEBUI_HOST","0.0.0.0"),int(os.getenv("WEBUI_PORT","8080"))),Handler);server.daemon_threads=True
 threading.Thread(target=server.serve_forever,daemon=True,name="webui").start();logging.info("Web UI listening on port %s (username %s)",os.getenv("WEBUI_PORT","8080"),AUTH_USERNAME);return server
