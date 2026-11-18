import http.cookiejar, os, re, shutil, subprocess, sys, tempfile, time, urllib.parse, urllib.request
from pathlib import Path

ROOT=Path(__file__).parent
DATA=Path(tempfile.mkdtemp(prefix='network-smoke-'))
PORT='18181'
env=os.environ.copy();env.update({'PORT':PORT,'PRODUCTION':'0','TRUST_PROXY':'0','DATA_DIR':str(DATA),'APP_SECRET':'smoke-test-secret-value','ADMIN_EMAILS':'owner@example.com','PBKDF2_ITERATIONS':'10000'})
p=subprocess.Popen([sys.executable,str(ROOT/'the_network_production.py')],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
BASE='http://127.0.0.1:'+PORT

def wait_ready():
    for _ in range(80):
        try:
            with urllib.request.urlopen(BASE+'/healthz',timeout=.5) as r:
                if r.status==200:return
        except Exception:time.sleep(.1)
    raise RuntimeError('server failed to start')

def client():
    jar=http.cookiejar.CookieJar();op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar));return op

def get(op,path):
    with op.open(BASE+path,timeout=3) as r:return r.status,r.headers,r.read().decode('utf-8','replace')

def post(op,path,data):
    body=urllib.parse.urlencode(data).encode();req=urllib.request.Request(BASE+path,data=body,method='POST',headers={'Content-Type':'application/x-www-form-urlencoded'})
    try:
        with op.open(req,timeout=4) as r:return r.status,r.headers,r.read().decode('utf-8','replace')
    except urllib.error.HTTPError as e:return e.code,e.headers,e.read().decode('utf-8','replace')

def csrf(html):
    m=re.search(r'name="csrf" value="([^"]+)"',html);return m.group(1) if m else ''

try:
    wait_ready()
    anon=client()
    st,h,home=get(anon,'/')
    assert st==200 and "script-src 'self';" in h['Content-Security-Policy'] and "unsafe-inline" not in h['Content-Security-Policy'].split('script-src',1)[1].split(';',1)[0]
    st,_,_=post(anon,'/register',{'name':'Owner','email':'owner@example.com','password':'correct-horse-battery-staple', 'confirm_password': 'correct-horse-battery-staple'})
    assert st==200
    st,_,body=post(anon,'/login',{'email':'nobody@example.com','password':'wrong-password-123'})
    assert st==401 and 'Email or password is incorrect' in body
    owner=client();st,_,_=post(owner,'/login',{'email':'owner@example.com','password':'correct-horse-battery-staple'});assert st==200
    st,_,page=get(owner,'/');token=csrf(page);assert token
    st,_,_=post(owner,'/posts',{'csrf':token,'body':'smoke test post'});assert st==200
    st,_,admin=get(owner,'/admin');assert st==200 and 'Admin dashboard' in admin

    user=client();post(user,'/register',{'name':'Private User','email':'private@example.com','password':'another-correct-password-123', 'confirm_password': 'another-correct-password-123'});post(user,'/login',{'email':'private@example.com','password':'another-correct-password-123'})
    st,_,page=get(user,'/');ut=csrf(page);assert ut
    post(user,'/profile',{'csrf':ut,'display_name':'Private User','bio':'ultrasecretbio','interests':'secretinterest'})
    st,_,page=get(user,'/settings');ut=csrf(page);post(user,'/settings',{'csrf':ut,'private_account':'on'})

    st,_,page=get(owner,'/people?q=ultrasecretbio')
    assert 'Private User' not in page
    st,_,page=get(owner,'/people?q=Private%20User')
    assert 'Private User' in page and 'ultrasecretbio' not in page and 'Private account' in page

    st,_,page=get(owner,'/');token=csrf(page)
    m=re.search(r'action="/delete"[^>]*>.*?name="post_id" value="(\d+)"',page,re.S);assert m
    post(owner,'/delete',{'csrf':token,'post_id':m.group(1)})
    st,_,admin=get(owner,'/admin');assert 'delete_post' in admin and 'smoke test post' in admin

    print('SMOKE TEST: PASS')
finally:
    p.terminate()
    try:p.wait(timeout=5)
    except subprocess.TimeoutExpired:p.kill();p.wait()
    out=p.stdout.read() if p.stdout else ''
    print(out[-3000:])
    shutil.rmtree(DATA,ignore_errors=True)
