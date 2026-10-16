#!/usr/bin/env python3
"""宠安星 PetSafe Star — dependency-free WSGI/SQLite MVP.
Run python app.py or gunicorn app:application for production behind HTTPS proxy.
"""
import os, re, html, json, sqlite3, secrets, hashlib, hmac, traceback
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from http import HTTPStatus
from urllib.parse import parse_qs, quote
from wsgiref.simple_server import make_server
from http.cookies import SimpleCookie
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('PETSAFE_DATA_DIR', str(ROOT/'data')))
DB = DATA/'petsafe.sqlite3'
DATA.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get('PORT', '8000'))
HOST = os.environ.get('HOST', '127.0.0.1')
COOKIE_SECURE = os.environ.get('COOKIE_SECURE', '0') == '1'
MAX_REQUEST = 128 * 1024
SERVICE_TYPES = {'boarding': ('家庭寄养', 128, '天'), 'feeding': ('上门喂养', 58, '次'), 'walking': ('遛宠服务', 48, '次')}
STATUS_LABEL = {'pending':'待确认', 'confirmed':'已确认', 'handoff':'交接完成', 'active':'照护中', 'completed':'已完成', 'cancelled':'已取消'}
ALERT_LABEL = {1:'1级 · 轻微',2:'2级 · 关注',3:'3级 · 紧急'}

def dbconn():
    con = sqlite3.connect(str(DB), timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=20000')
    return con

def pwdhash(p, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', p.encode(), bytes.fromhex(salt), 260000).hex()
    return salt + '$' + digest

def pwcheck(p, stored):
    try:
        salt, digest = stored.split('$', 1)
        return hmac.compare_digest(pwdhash(p, salt), stored)
    except Exception:
        return False

def init_db():
    with dbconn() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, csrf TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pets(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL, species TEXT NOT NULL, age TEXT, notes TEXT NOT NULL DEFAULT '', emergency_contact TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS caregivers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL, bio TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), pet_id INTEGER NOT NULL REFERENCES pets(id), caregiver_id INTEGER REFERENCES caregivers(id), service TEXT NOT NULL, start_date TEXT NOT NULL, qty INTEGER NOT NULL, requirements TEXT NOT NULL, addons TEXT NOT NULL DEFAULT '', total_cents INTEGER NOT NULL, deposit_cents INTEGER NOT NULL, deposit_status TEXT NOT NULL DEFAULT '未支付（演示，无支付接口）', status TEXT NOT NULL DEFAULT 'pending', owner_confirmed INTEGER NOT NULL DEFAULT 0, caregiver_confirmed INTEGER NOT NULL DEFAULT 0, envelope_read INTEGER NOT NULL DEFAULT 0, handoff_notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS diary(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), actor_id INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), actor_id INTEGER NOT NULL REFERENCES users(id), level INTEGER NOT NULL, detail TEXT NOT NULL, status TEXT NOT NULL DEFAULT '待人工复核', resolution TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor_id INTEGER REFERENCES users(id), action TEXT NOT NULL, target TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        ''')
        if not c.execute('SELECT id FROM caregivers LIMIT 1').fetchone():
            c.executemany('INSERT INTO caregivers(name,city,bio,approved,active) VALUES(?,?,?,?,?)', [
                ('示例宠托师 · 小林','示例城区','家庭寄养 · 待平台审核',0,1),
                ('示例宠托师 · 阿宁','示例城区','上门喂养与遛宠 · 待平台审核',0,1)
            ])
        email = os.environ.get('ADMIN_EMAIL','').strip().lower()
        password = os.environ.get('ADMIN_PASSWORD','')
        if email and password and not c.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone():
            if len(password) < 12: raise RuntimeError('ADMIN_PASSWORD 至少12位')
            c.execute('INSERT INTO users(email,password,name,role) VALUES(?,?,?,?)', (email,pwdhash(password),'平台管理员','admin'))
            print('Created initial admin:', email)

init_db()

def esc(s): return html.escape(str(s if s is not None else ''), quote=True)
def money(cents): return f'¥{cents/100:,.2f}'
def tr(s): return esc(s)
def now(): return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
def url(path): return path

def page(title, body, user=None, notice='', csrf='', admin=False):
    nav = '''<a href="/">首页</a><a href="/services">找服务</a>'''
    if user:
        nav += '<a href="/pets">我的宠物</a><a href="/orders">我的订单</a>'
        if user['role']=='admin': nav += '<a class="pill" href="/admin">运营后台</a>'
        nav += '<span class="user">'+esc(user['name'])+'</span><form method="post" action="/logout" class="inline"><input type="hidden" name="csrf" value="'+esc(csrf)+'"><button class="linkbtn">退出</button></form>'
    else: nav += '<a href="/login">登录</a><a class="pill" href="/register">注册</a>'
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="宠安星 · 透明可追溯的宠物寄养照护平台"><title>{esc(title)} · 宠安星</title><link rel="stylesheet" href="/static/style.css"></head><body><header class="site-head"><a href="/" class="brand"><span class="brand-icon">✦</span><span>宠安星 <small>PETSAFE STAR</small></span></a><button class="menu-btn" type="button" aria-label="展开菜单" aria-expanded="false" onclick="toggleMenu(this)">☰</button><nav class="nav" id="main-nav">{nav}</nav></header><main class="container">{f'<div class="notice">{esc(notice)}</div>' if notice else ''}{body}</main><footer><span>© 2026 XBAOS · 宠安星 PETSAFE STAR · 版权所有</span><span>演示与试运行系统 · 无真实支付或保险承保</span></footer><script src="/static/app.js"></script></body></html>'''

def csrf_field(token): return '<input type="hidden" name="csrf" value="'+esc(token)+'">'
def badge(text, tone=''): return f'<span class="badge {tone}">{esc(text)}</span>'
def section(heading, contents): return '<section class="card"><h2>'+esc(heading)+'</h2>'+contents+'</section>'
def buttons(path, token, label, extra='', confirm=''): return f'<form method="post" action="{esc(path)}" class="inline">{csrf_field(token)}{extra}<button class="btn small" {("data-confirm="+chr(34)+esc(confirm)+chr(34)) if confirm else ""}>{esc(label)}</button></form>'
def failure(message, status='400 Bad Request'): raise AppError(message, status)
class AppError(Exception):
    def __init__(self, msg, status='400 Bad Request'): super().__init__(msg); self.status=status

def select_auth(environ, conn):
    cookie = SimpleCookie()
    try: cookie.load(environ.get('HTTP_COOKIE',''))
    except Exception: return None,None,None
    token = cookie.get('ps_session')
    if not token:return None,None,None
    raw = token.value
    if not re.fullmatch('[a-f0-9]{64}',raw):return None,None,None
    row = conn.execute('SELECT u.*, s.csrf, s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?',(hashlib.sha256(raw.encode()).hexdigest(),)).fetchone()
    if not row or row['expires_at'] < now():return None,None,None
    return row, row['csrf'], raw

def cookie_header(token='', clear=False):
    val = 'ps_session='+('' if clear else token)+'; Path=/; HttpOnly; SameSite=Lax'
    if COOKIE_SECURE:val+='; Secure'
    if clear:val+='; Max-Age=0'
    else:val+='; Max-Age=604800'
    return ('Set-Cookie',val)

def getform(env):
    size=int(env.get('CONTENT_LENGTH','0') or 0)
    if size > MAX_REQUEST: failure('请求内容太大','413 Payload Too Large')
    if env.get('CONTENT_TYPE','').split(';')[0] != 'application/x-www-form-urlencoded':failure('表单格式不受支持')
    body=env['wsgi.input'].read(size).decode('utf-8','replace')
    return {k:v[0] for k,v in parse_qs(body,keep_blank_values=True,max_num_fields=50).items()}

def view_home(user,csrf):
    offers=''.join(f'''<article class="service"><div class="emoji">{icon}</div><h3>{name}</h3><p>{desc}</p><div class="row"><strong>参考价 {money(price*100)}/{unit}</strong><a class="btn small" href="/book?service={key}">立即预约 →</a></div></article>''' for (key,(name,price,unit)),icon,desc in zip(SERVICE_TYPES.items(),['🏡','🥣','🦮'],['住得安心，每日照护看得见','在熟悉的家里，按时照料','关键节点留痕，活动有记录']))
    return f'''<div class="hero"><div><div class="eyebrow">PETSAFE STAR · 透明宠物照护</div><h1>每一次托付，<br><em>都值得被认真记录。</em></h1><p>预约、交接、照护打卡、异常提醒与服务复核，一个平台串起照护全过程。</p><div class="actions"><a class="btn" href="/services">探索服务 →</a><a class="btn ghost" href="/orders">查看我的订单</a></div><div class="hero-tags">✓ 标准化照护&nbsp;&nbsp; ✓ 过程可追溯&nbsp;&nbsp; ✓ 责任有边界</div></div><div class="hero-art"><div class="big-paw">🐾</div><div class="float-note">✦ 每一次守护，都有迹可循</div></div></div><div class="section-title"><div><span class="eyebrow">OUR SERVICES</span><h2>三大贴心照护服务</h2></div><p>从预约开始，让爱不缺席</p></div><div class="grid three">{offers}</div><div class="section-title"><div><span class="eyebrow">HOW IT WORKS</span><h2>透明照护 · 全程可见</h2></div></div><div class="steps"><div><b>01</b><h3>建立宠物档案</h3><p>记录健康信息和生活习惯</p></div><div><b>02</b><h3>预约与费用确认</h3><p>服务、押金及规则事先说明</p></div><div><b>03</b><h3>交接与日常打卡</h3><p>关键节点有记录可查</p></div><div><b>04</b><h3>异常通知与复核</h3><p>人工处理，证据留存</p></div></div><div class="callout"><div><h2>放心，是可以被看见的。</h2><p>视频交接是设计目标；当前版本提供文字交接确认与时间线记录，视频直播尚未接入。</p></div><a class="btn" href="/register">开始使用</a></div>'''

def view_services(conn):
    caregivers=conn.execute('SELECT * FROM caregivers WHERE approved=1 AND active=1').fetchall()
    card=''.join(f'<div class="card"><h3>{esc(row["name"])}</h3><p>{esc(row["city"])} · {esc(row["bio"])}</p>{badge("审核通过","ok")}</div>' for row in caregivers) or '<div class="muted card">目前暂无审核通过的宠托师。可先体验预约流程，订单将等待人工确认。</div>'
    return '<div class="page-head"><span class="eyebrow">DISCOVER</span><h1>找到适合的照护服务</h1><p>价格仅为试运行参考价，以实际确认价格和服务协议为准。</p></div><div class="grid three">'+''.join(f'<div class="card"><h2>{name}</h2><p>{money(price*100)} / {unit}</p><a class="btn" href="/book?service={key}">预约服务</a></div>' for key,(name,price,unit) in SERVICE_TYPES.items())+'</div><h2 class="space-top">已审核服务者</h2><div class="grid two">'+card+'</div>'

def auth_form(kind,csrf='',notice=''):
    reg=kind=='register'; target='/register' if reg else '/login'
    return f'''<div class="auth-wrap"><section class="card auth"><div class="eyebrow">PETSAFE STAR</div><h1>{'创建宠安星账号' if reg else '欢迎回来'}</h1><p>{'开始记录每一次安心托付' if reg else '登录后管理您的宠物和订单'}</p><form action="{target}" method="post">{csrf_field(csrf) if csrf else ''}{'<label>称呼<input name="name" required maxlength="40" placeholder="您的称呼"></label>' if reg else ''}<label>邮箱<input name="email" type="email" required maxlength="120" autocomplete="email" placeholder="name@example.com"></label><label>密码<input name="password" type="password" minlength="8" required autocomplete="{'new-password' if reg else 'current-password'}"></label><button class="btn wide">{'立即注册' if reg else '登录'}</button></form><p class="hint">{'已有账号？<a href="/login">去登录</a>' if reg else '还没有账号？<a href="/register">去注册</a>'}</p></section></div>'''

def require(user,role=None):
    if not user:failure('请先登录','401 Unauthorized')
    if role and user['role']!=role:failure('没有操作权限','403 Forbidden')

def pet_options(conn,user):
    rows=conn.execute('SELECT id,name,species FROM pets WHERE user_id=? ORDER BY id DESC',(user['id'],)).fetchall()
    return rows

def pets_view(conn,user,csrf):
    rows=pet_options(conn,user)
    cards=''.join(f'<div class="card pet-card"><div class="emoji">{"🐈" if r["species"]=="猫" else "🐕" if r["species"]=="狗" else "🐾"}</div><h3>{esc(r["name"])}</h3><p>{esc(r["species"])} · {esc(r["age"] or "年龄未填")}</p><p>{esc(r["notes"] or "暂无照护备注")}</p><p class="muted">紧急联系人：{esc(r["emergency_contact"] or "未填写")}</p></div>' for r in conn.execute('SELECT * FROM pets WHERE user_id=? ORDER BY id DESC',(user['id'],))) or '<div class="card muted">还没有宠物档案，请在下方新建。</div>'
    form=f'''<form method="post" action="/pets/add" class="form-grid">{csrf_field(csrf)}<label>宠物名字<input name="name" maxlength="40" required></label><label>宠物类型<select name="species"><option>猫</option><option>狗</option><option>其他</option></select></label><label>年龄<input name="age" maxlength="30" placeholder="如：2岁"></label><label>紧急联系人<input name="emergency_contact" maxlength="100" placeholder="姓名及电话"></label><label class="full">健康申报、过敏、用药及离家小信封<textarea name="notes" maxlength="2000" placeholder="生活习惯、忌口、安抚方式、已知健康状况"></textarea></label><button class="btn">保存宠物档案</button></form>'''
    return '<div class="page-head"><h1>我的宠物档案</h1><p>在预约前完善健康信息与紧急联系方式。</p></div><div class="grid three">'+cards+'</div>'+section('添加宠物',form)

def book_view(conn,user,csrf,service='boarding'):
    if service not in SERVICE_TYPES:service='boarding'
    pets=pet_options(conn,user)
    options=''.join(f'<option value="{p["id"]}">{esc(p["name"])} · {esc(p["species"])}</option>' for p in pets)
    if not pets:return '<div class="page-head"><h1>先建立宠物档案</h1></div><div class="card"><p>预约需要明确对应的宠物和健康照护信息。</p><a class="btn" href="/pets">添加我的宠物 →</a></div>'
    caregivers=conn.execute('SELECT * FROM caregivers WHERE approved=1 AND active=1').fetchall()
    careopt='<option value="">由平台安排（待确认）</option>'+''.join(f'<option value="{x["id"]}">{esc(x["name"])} / {esc(x["city"])}</option>' for x in caregivers)
    opts=''.join(f'<option value="{key}" {"selected" if key==service else ""} data-price="{price}">{name} · {price}元/{unit}</option>' for key,(name,price,unit) in SERVICE_TYPES.items())
    return f'''<div class="page-head"><span class="eyebrow">BOOK YOUR CARE</span><h1>预约照护服务</h1><p>费用、押金和服务要求都在下单前确认。此版本不收款。</p></div><div class="grid two booking"><section class="card"><h2>填写预约信息</h2><form method="post" action="/book">{csrf_field(csrf)}<label>服务类型<select id="service" name="service" required>{opts}</select></label><label>选择宠物<select name="pet_id" required>{options}</select></label><label>服务日期<input name="start_date" type="date" required min="{date.today().isoformat()}"></label><label>服务天数 / 次数<input id="qty" name="qty" type="number" value="1" min="1" max="30" required></label><label>宠托师<select name="caregiver_id">{careopt}</select></label><label>照护要求<textarea name="requirements" maxlength="1000" placeholder="喂食时间、用品交接、过敏提醒等"></textarea></label><label class="check"><input type="checkbox" required name="agree" value="yes">已阅读并理解费用、押金规则和服务需人工确认的说明</label><button class="btn wide">提交预约申请</button></form></section><aside class="card price-panel"><h2>费用预览</h2><div class="price-line"><span>服务费</span><strong id="total-preview">¥128.00</strong></div><div class="price-line"><span>预约担保押金 30%</span><strong id="deposit-preview">¥38.40</strong></div><div class="divider"></div><p>押金为订单金额的30%，信用免押需另行审核。本演示系统仅记录押金应付金额，<b>不提供真实扣款和退款</b>。</p><p>取消、超时及损坏处理须依据双方协议与订单证据；保险尚未默认生效。</p><p class="muted">预约提交后，平台审核并确认服务。</p></aside></div>'''

def myorders(conn,user):
    rows=conn.execute('SELECT o.*,p.name pet_name FROM orders o JOIN pets p ON p.id=o.pet_id WHERE o.user_id=? ORDER BY o.id DESC',(user['id'],)).fetchall()
    result=''.join(f'''<a class="order-card" href="/orders/{r['id']}"><div><small>订单 #{r['id']} · {esc(r['start_date'])}</small><h3>{esc(SERVICE_TYPES[r['service']][0])} · {esc(r['pet_name'])}</h3><span class="muted">{r['qty']} {SERVICE_TYPES[r['service']][2]} · 总价 {money(r['total_cents'])}</span></div>{badge(STATUS_LABEL.get(r['status'],r['status']))}<span class="arrow">→</span></a>''' for r in rows) or '<div class="card muted">暂无订单，去预约第一份安心照护吧。</div>'
    return '<div class="page-head"><h1>我的订单</h1><p>每个订单的交接、照护记录与异常处理均归档在这里。</p></div><div class="stack">'+result+'</div>'

def get_order(conn,oid,user):
    r=conn.execute('SELECT o.*,p.name pet_name,p.notes pet_notes, u.name owner_name FROM orders o JOIN pets p ON p.id=o.pet_id JOIN users u ON u.id=o.user_id WHERE o.id=?',(oid,)).fetchone()
    if not r:failure('订单不存在','404 Not Found')
    if user['role']!='admin' and r['user_id']!=user['id']:failure('无权查看该订单','403 Forbidden')
    return r

def order_view(conn,user,csrf,oid):
    r=get_order(conn,oid,user)
    logs=conn.execute('SELECT d.*,u.name actor FROM diary d JOIN users u ON u.id=d.actor_id WHERE d.order_id=? ORDER BY d.id DESC',(oid,)).fetchall()
    alerts=conn.execute('SELECT * FROM alerts WHERE order_id=? ORDER BY id DESC',(oid,)).fetchall()
    timeline=''.join(f'<div class="timeline-item"><small>{esc(d["created_at"])} · {esc(d["actor"])}</small><h4>{esc(d["action"])}</h4><p>{esc(d["detail"])}</p></div>' for d in logs) or '<p class="muted">尚无照护记录。</p>'
    issues=''.join(f'<div class="alert"><strong>{esc(ALERT_LABEL.get(a["level"],"异常"))} · {esc(a["status"])}</strong><p>{esc(a["detail"])}</p><small>{esc(a["created_at"])} · 处理：{esc(a["resolution"] or "待人工核查")}</small></div>' for a in alerts) or '<p class="muted">暂无异常上报。</p>'
    task_form=f'''<form method="post" action="/orders/{oid}/diary">{csrf_field(csrf)}<label>记录类型<select name="action"><option>喂食</option><option>换水</option><option>清洁</option><option>状态巡查</option><option>遛宠</option><option>其他</option></select></label><label>内容<textarea name="detail" maxlength="1000" required placeholder="记录实际观察，勿进行医学诊断"></textarea></label><button class="btn">提交时间戳记录</button></form>'''
    alert_form=f'''<form method="post" action="/orders/{oid}/alert">{csrf_field(csrf)}<label>异常等级<select name="level"><option value="1">1级 · 打卡延迟等</option><option value="2">2级 · 进食量偏低等</option><option value="3">3级 · 受伤、呕吐等紧急情况</option></select></label><label>实际情况<textarea name="detail" maxlength="1000" required></textarea></label><button class="btn secondary">提交异常，等待人工处理</button></form>'''
    handoff=f'''<p>离家小信封：{badge('已阅读' if r['envelope_read'] else '未确认','ok' if r['envelope_read'] else '')}</p><p>主人交接：{badge('已确认' if r['owner_confirmed'] else '待确认')}</p><p>宠托师交接：{badge('已确认' if r['caregiver_confirmed'] else '待确认')}</p><p>交接观察：{esc(r['handoff_notes'] or '尚未记录')}</p>'''
    if user['role']=='admin':
        handoff+=f'''<form method="post" action="/orders/{oid}/handoff">{csrf_field(csrf)}<label>交接观察记录<textarea name="notes" maxlength="1500" placeholder="仅记录可观察状态，不替代诊断">{esc(r['handoff_notes'])}</textarea></label><label class="check"><input type="checkbox" name="envelope" value="yes" {'checked' if r['envelope_read'] else ''}>已经阅读并确认离家小信封</label><label class="check"><input type="checkbox" name="caregiver" value="yes" {'checked' if r['caregiver_confirmed'] else ''}>服务方完成交接确认</label><button class="btn small">保存交接记录</button></form>'''
    elif not r['owner_confirmed']:
        handoff+=buttons(f'/orders/{oid}/owner-confirm',csrf,'主人确认交接','', '确认已核对交接记录？')
    info=f'''<div class="summary-grid"><div><span>服务类型</span><b>{esc(SERVICE_TYPES[r['service']][0])}</b></div><div><span>宠物</span><b>{esc(r['pet_name'])}</b></div><div><span>日期 / 数量</span><b>{esc(r['start_date'])} · {r['qty']}</b></div><div><span>总费用</span><b>{money(r['total_cents'])}</b></div><div><span>担保押金 (30%)</span><b>{money(r['deposit_cents'])}</b></div><div><span>押金状态</span><b>{esc(r['deposit_status'])}</b></div></div><p>照护要求：{esc(r['requirements'] or '无特殊要求')}</p><p class="hint">视频直播、在线支付和自动理赔尚未接入；文字记录不是影像证据。</p>'''
    return f'''<div class="page-head"><a href="{'/admin/orders' if user['role']=='admin' else '/orders'}" class="muted">← 返回订单列表</a><h1>订单 #{oid} {badge(STATUS_LABEL.get(r['status'],r['status']))}</h1><p>创建时间 {esc(r['created_at'])}</p></div>{section('服务与收费明细',info)}<div class="grid two">{section('交接验宠 · 双方确认',handoff)}{section('照护日记 · 时间线',timeline+task_form if user['role']=='admin' else timeline)}</div>{section('异常上报与人工复核',issues+alert_form)}'''

def admin_view(conn,user,csrf):
    require(user,'admin')
    stats={k:conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for k,table in [('orders','orders'),('pets','pets'),('users','users'),('alerts','alerts')]}
    awaiting=conn.execute("SELECT COUNT(*) FROM alerts WHERE status='待人工复核'").fetchone()[0]
    return f'''<div class="page-head"><div class="eyebrow">OPERATIONS CONSOLE</div><h1>运营管理后台</h1><p>管理员专属权限 · 数据存储于服务端 SQLite 数据库</p></div><div class="grid four">{''.join(f'<div class="stat"><small>{k}</small><strong>{v}</strong></div>' for k,v in [('订单总数',stats['orders']),('宠物档案',stats['pets']),('注册用户',stats['users']),('待处理异常',awaiting)])}</div><div class="grid three space-top"><a class="admin-tile" href="/admin/orders"><span>📦</span><h3>订单管理</h3><p>状态流转、交接记录与时间线</p>→</a><a class="admin-tile" href="/admin/caregivers"><span>👩‍⚕️</span><h3>宠托师审核</h3><p>审批、启停与服务者建档</p>→</a><a class="admin-tile" href="/admin/alerts"><span>🔔</span><h3>异常人工复核</h3><p>查看分级事件和处置意见</p>→</a></div><div class="callout"><div><h2>运营边界</h2><p>当前版本为可部署的试运行 MVP，未接入支付、视频监控、短信通知、保险和自动医疗判断。</p></div></div>'''

def admin_orders(conn,csrf):
    rows=conn.execute('SELECT o.*,p.name pet_name,u.email FROM orders o JOIN pets p ON p.id=o.pet_id JOIN users u ON u.id=o.user_id ORDER BY o.id DESC LIMIT 100').fetchall()
    out=''.join(f'''<div class="manage-row"><div><a href="/orders/{r['id']}"><b>#{r['id']} · {esc(r['pet_name'])} · {esc(SERVICE_TYPES[r['service']][0])}</b></a><p>{esc(r['email'])} · {esc(r['start_date'])} · {money(r['total_cents'])}</p>{badge(STATUS_LABEL.get(r['status'],r['status']))}</div><form method="post" action="/admin/orders/{r['id']}/status" class="inline manage-form">{csrf_field(csrf)}<select name="status">{''.join(f'<option value="{key}" {"selected" if key==r["status"] else ""}>{label}</option>' for key,label in STATUS_LABEL.items())}</select><button class="btn small">更新状态</button></form></div>''' for r in rows) or '<p class="muted">暂无订单。</p>'
    return '<div class="page-head"><a href="/admin">← 运营后台</a><h1>订单管理</h1></div>'+section('订单列表 · 最近100笔',out)

def admin_caregivers(conn,csrf):
    rows=conn.execute('SELECT * FROM caregivers ORDER BY id DESC').fetchall()
    out=''.join(f'''<div class="manage-row"><div><b>{esc(r['name'])}</b><p>{esc(r['city'])} · {esc(r['bio'])}</p>{badge('审核通过' if r['approved'] else '待审核','ok' if r['approved'] else '')} {badge('接单中' if r['active'] else '已停用')}</div><form method="post" action="/admin/caregivers/{r['id']}/toggle" class="inline">{csrf_field(csrf)}<button class="btn small">{'撤销审核' if r['approved'] else '审核通过'}</button></form><form method="post" action="/admin/caregivers/{r['id']}/active" class="inline">{csrf_field(csrf)}<button class="btn small secondary">{'暂停接单' if r['active'] else '恢复接单'}</button></form></div>''' for r in rows)
    add=f'''<form action="/admin/caregivers/add" method="post" class="form-grid">{csrf_field(csrf)}<label>服务者名称<input name="name" maxlength="80" required></label><label>城市 / 区域<input name="city" maxlength="80" required></label><label class="full">经验与服务范围<input name="bio" maxlength="300" required></label><button class="btn">创建待审核服务者</button></form>'''
    return '<div class="page-head"><a href="/admin">← 运营后台</a><h1>宠托师管理</h1></div>'+section('审核与接单状态',out)+section('新增服务者（默认待审核）',add)

def admin_alerts(conn,csrf):
    rows=conn.execute('SELECT a.*,o.user_id FROM alerts a JOIN orders o ON o.id=a.order_id ORDER BY CASE WHEN a.status="待人工复核" THEN 0 ELSE 1 END,a.level DESC,a.id DESC LIMIT 100').fetchall()
    out=''.join(f'''<div class="manage-row"><div><b>订单 <a href="/orders/{r['order_id']}">#{r['order_id']}</a> · {esc(ALERT_LABEL[r['level']])}</b><p>{esc(r['detail'])}</p><small>{esc(r['created_at'])}</small><p>{badge(r['status'])} {esc(r['resolution'])}</p></div><form method="post" action="/admin/alerts/{r['id']}/resolve" class="manage-form">{csrf_field(csrf)}<input name="resolution" required maxlength="1000" placeholder="人工核实及处理意见" value="{esc(r['resolution'])}"><button class="btn small">标记已复核</button></form></div>''' for r in rows) or '<p class="muted">暂无异常。</p>'
    return '<div class="page-head"><a href="/admin">← 运营后台</a><h1>异常复核工作台</h1><p>AI 或系统提示不等于医疗诊断；三级紧急情况需立即线下联系并采取应急措施。</p></div>'+section('异常事件 · 最近100条',out)

def login_session(c,user_id):
    raw=secrets.token_hex(32); csrf=secrets.token_hex(24)
    c.execute('INSERT INTO sessions(token_hash,user_id,csrf,expires_at) VALUES(?,?,?,?)',(hashlib.sha256(raw.encode()).hexdigest(),user_id,csrf,(datetime.utcnow()+timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')))
    return raw

def int_id(text):
    try: n=int(text)
    except Exception: failure('参数不正确')
    if n<=0:failure('参数不正确')
    return n

def application(environ,start_response):
    method=environ.get('REQUEST_METHOD','GET').upper(); path=environ.get('PATH_INFO','/')
    headers=[('Content-Type','text/html; charset=utf-8'),('X-Content-Type-Options','nosniff'),('X-Frame-Options','DENY'),('Referrer-Policy','same-origin'),('Cache-Control','no-store')]
    status='200 OK'; payload=b''
    try:
        if path in ('/static/style.css','/static/app.js'):
            f=ROOT/path.lstrip('/')
            content=f.read_bytes(); headers=[('Content-Type','text/css; charset=utf-8' if path.endswith('.css') else 'text/javascript; charset=utf-8'),('Cache-Control','public, max-age=3600'),('X-Content-Type-Options','nosniff')]
            start_response(status,headers);return [content]
        if path=='/health':
            with dbconn() as c:c.execute('SELECT 1').fetchone()
            start_response(status,[('Content-Type','text/plain; charset=utf-8')]);return [b'ok']
        if method not in ('GET','POST'):failure('方法不支持','405 Method Not Allowed')
        with dbconn() as conn:
            user,csrf,token=select_auth(environ,conn)
            form=getform(environ) if method=='POST' else {}
            if method=='POST' and path not in ('/login','/register'):
                require(user)
                if not csrf or not hmac.compare_digest(form.get('csrf',''),csrf):failure('页面已过期，请刷新后重试','403 Forbidden')
            def move(dest,cookie=None):
                nonlocal status,headers
                status='303 See Other';headers.append(('Location',dest))
                if cookie:headers.append(cookie)
                return b''
            if path=='/' and method=='GET':body=view_home(user,csrf)
            elif path=='/services' and method=='GET':body=view_services(conn)
            elif path=='/register' and method=='GET':body=auth_form('register')
            elif path=='/login' and method=='GET':body=auth_form('login')
            elif path=='/register' and method=='POST':
                email=form.get('email','').strip().lower();password=form.get('password','');name=form.get('name','').strip()
                if not re.fullmatch(r'[^@\s]{1,70}@[^@\s]{1,70}\.[^@\s]{2,20}',email) or not 8<=len(password)<=128 or not 1<=len(name)<=40:failure('请输入有效邮箱、称呼和至少8位密码')
                try:
                    with conn:cur=conn.execute('INSERT INTO users(email,password,name) VALUES(?,?,?)',(email,pwdhash(password),name))
                except sqlite3.IntegrityError:failure('该邮箱已注册')
                with conn: raw=login_session(conn,cur.lastrowid)
                payload=move('/pets',cookie_header(raw));body=None
            elif path=='/login' and method=='POST':
                email=form.get('email','').strip().lower();password=form.get('password','')
                found=conn.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
                if not found or not pwcheck(password,found['password']):failure('邮箱或密码错误','401 Unauthorized')
                with conn:raw=login_session(conn,found['id'])
                payload=move('/admin' if found['role']=='admin' else '/orders',cookie_header(raw));body=None
            elif path=='/logout' and method=='POST':
                with conn:conn.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
                payload=move('/',cookie_header(clear=True));body=None
            elif path=='/pets' and method=='GET':
                require(user);body=pets_view(conn,user,csrf)
            elif path=='/pets/add' and method=='POST':
                name=form.get('name','').strip();species=form.get('species','');age=form.get('age','').strip();notes=form.get('notes','').strip();contact=form.get('emergency_contact','').strip()
                if not 1<=len(name)<=40 or species not in ('猫','狗','其他') or len(notes)>2000 or len(contact)>100 or len(age)>30:failure('宠物信息不完整或超长')
                with conn:conn.execute('INSERT INTO pets(user_id,name,species,age,notes,emergency_contact) VALUES(?,?,?,?,?,?)',(user['id'],name,species,age,notes,contact))
                payload=move('/pets');body=None
            elif path=='/book' and method=='GET':
                require(user);query=parse_qs(environ.get('QUERY_STRING',''));body=book_view(conn,user,csrf,query.get('service',['boarding'])[0])
            elif path=='/book' and method=='POST':
                require(user);svc=form.get('service','');qty=int_id(form.get('qty','0'));pid=int_id(form.get('pet_id','0'));start=form.get('start_date','');requirements=form.get('requirements','').strip()
                if svc not in SERVICE_TYPES or qty>30 or len(requirements)>1000 or form.get('agree')!='yes':failure('预约信息不完整')
                try: day=date.fromisoformat(start)
                except ValueError:failure('请选择有效日期')
                if not date.today()<=day<=date.today()+timedelta(days=365):failure('预约日期需在未来一年内')
                if not conn.execute('SELECT 1 FROM pets WHERE id=? AND user_id=?',(pid,user['id'])).fetchone():failure('宠物档案不存在','403 Forbidden')
                care=form.get('caregiver_id','').strip();careid=None
                if care:
                    careid=int_id(care)
                    if not conn.execute('SELECT 1 FROM caregivers WHERE id=? AND approved=1 AND active=1',(careid,)).fetchone():failure('此服务者暂不可预约')
                total=SERVICE_TYPES[svc][1]*qty*100
                deposit=int((Decimal(total)*Decimal('0.30')).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
                with conn:
                    cur=conn.execute('INSERT INTO orders(user_id,pet_id,caregiver_id,service,start_date,qty,requirements,total_cents,deposit_cents) VALUES(?,?,?,?,?,?,?,?,?)',(user['id'],pid,careid,svc,start,qty,requirements,total,deposit))
                    conn.execute('INSERT INTO audit(actor_id,action,target) VALUES(?,?,?)',(user['id'],'创建预约',str(cur.lastrowid)))
                payload=move('/orders/'+str(cur.lastrowid));body=None
            elif path=='/orders' and method=='GET':
                require(user);body=myorders(conn,user)
            elif re.fullmatch(r'/orders/\d+',path) and method=='GET':
                require(user);body=order_view(conn,user,csrf,int(path.split('/')[-1]))
            elif re.fullmatch(r'/orders/\d+/(diary|alert|owner-confirm|handoff)',path) and method=='POST':
                oid=int(path.split('/')[2]);act=path.split('/')[3];r=get_order(conn,oid,user)
                with conn:
                    if act=='diary':
                        require(user,'admin');action=form.get('action','');detail=form.get('detail','').strip()
                        if action not in ('喂食','换水','清洁','状态巡查','遛宠','其他') or not 1<=len(detail)<=1000:failure('请输入有效打卡内容')
                        conn.execute('INSERT INTO diary(order_id,actor_id,action,detail) VALUES(?,?,?,?)',(oid,user['id'],action,detail))
                    elif act=='alert':
                        level=int_id(form.get('level','0'));detail=form.get('detail','').strip()
                        if level>3 or not 1<=len(detail)<=1000:failure('异常记录不完整')
                        conn.execute('INSERT INTO alerts(order_id,actor_id,level,detail) VALUES(?,?,?,?)',(oid,user['id'],level,detail))
                    elif act=='owner-confirm':
                        if user['role']=='admin':failure('主人须自行确认','403 Forbidden')
                        conn.execute('UPDATE orders SET owner_confirmed=1 WHERE id=?',(oid,))
                    else:
                        require(user,'admin');notes=form.get('notes','').strip()
                        if len(notes)>1500:failure('交接记录过长')
                        conn.execute('UPDATE orders SET handoff_notes=?,envelope_read=?,caregiver_confirmed=? WHERE id=?',(notes,1 if form.get('envelope')=='yes' else 0,1 if form.get('caregiver')=='yes' else 0,oid))
                    conn.execute('INSERT INTO audit(actor_id,action,target) VALUES(?,?,?)',(user['id'],act,'order:'+str(oid)))
                payload=move('/orders/'+str(oid));body=None
            elif path=='/admin' and method=='GET':body=admin_view(conn,user,csrf)
            elif path=='/admin/orders' and method=='GET':
                require(user,'admin');body=admin_orders(conn,csrf)
            elif path=='/admin/caregivers' and method=='GET':
                require(user,'admin');body=admin_caregivers(conn,csrf)
            elif path=='/admin/alerts' and method=='GET':
                require(user,'admin');body=admin_alerts(conn,csrf)
            elif path=='/admin/caregivers/add' and method=='POST':
                require(user,'admin');name=form.get('name','').strip();city=form.get('city','').strip();bio=form.get('bio','').strip()
                if not (1<=len(name)<=80 and 1<=len(city)<=80 and 1<=len(bio)<=300):failure('请完整填写服务者信息')
                with conn:conn.execute('INSERT INTO caregivers(name,city,bio) VALUES(?,?,?)',(name,city,bio))
                payload=move('/admin/caregivers');body=None
            elif re.fullmatch(r'/admin/caregivers/\d+/(toggle|active)',path) and method=='POST':
                require(user,'admin');idx=int(path.split('/')[3]);col='approved' if path.endswith('/toggle') else 'active'
                with conn:conn.execute(f'UPDATE caregivers SET {col}=1-{col} WHERE id=?',(idx,))
                payload=move('/admin/caregivers');body=None
            elif re.fullmatch(r'/admin/orders/\d+/status',path) and method=='POST':
                require(user,'admin');idx=int(path.split('/')[3]);new=form.get('status','')
                if new not in STATUS_LABEL:failure('无效的订单状态')
                r=conn.execute('SELECT * FROM orders WHERE id=?',(idx,)).fetchone()
                if not r:failure('订单不存在','404 Not Found')
                if new in ('handoff','active','completed') and not(r['envelope_read'] and r['owner_confirmed'] and r['caregiver_confirmed']):failure('必须先阅读小信封并完成双方交接确认')
                if new=='completed' and not conn.execute('SELECT 1 FROM diary WHERE order_id=?',(idx,)).fetchone():failure('至少需要一条照护打卡记录才能完结')
                with conn:conn.execute('UPDATE orders SET status=? WHERE id=?',(new,idx))
                payload=move('/admin/orders');body=None
            elif re.fullmatch(r'/admin/alerts/\d+/resolve',path) and method=='POST':
                require(user,'admin');idx=int(path.split('/')[3]);resolution=form.get('resolution','').strip()
                if not 1<=len(resolution)<=1000:failure('请填写人工复核意见')
                with conn:conn.execute("UPDATE alerts SET status='已人工复核',resolution=? WHERE id=?",(resolution,idx))
                payload=move('/admin/alerts');body=None
            else: failure('页面不存在','404 Not Found')
            if body is not None:payload=page('宠安星' if path=='/' else '服务平台',body,user,csrf=csrf).encode('utf-8')
    except AppError as exc:
        status=exc.status
        msg=esc(str(exc))
        payload=page('操作提示',f'<section class="card error"><h1>操作未完成</h1><p>{msg}</p><a class="btn" href="/">返回首页</a></section>').encode('utf-8')
    except Exception:
        traceback.print_exc()
        status='500 Internal Server Error';payload=page('系统错误','<section class="card"><h1>系统暂时不可用</h1><p>请稍后再试，或联系管理员查看服务器日志。</p></section>').encode('utf-8')
    start_response(status,headers)
    return [payload]

if __name__=='__main__':
    print(f'PetSafe Star running at http://{HOST}:{PORT} ; DB={DB}')
    with make_server(HOST,PORT,application) as httpd:httpd.serve_forever()
