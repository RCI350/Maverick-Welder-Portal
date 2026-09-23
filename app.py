import json, io, os, re, sys, traceback, hashlib, hmac, uuid
from datetime import datetime, date
from functools import wraps
import psycopg
from psycopg.rows import dict_row
import resend as resend_lib
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, jsonify, Response)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from config import DATABASE_URL, SECRET_KEY, RESEND_API_KEY, WEBHOOK_URL, WEBHOOK_SECRET

app = Flask(__name__, template_folder='app/templates', static_folder='app/static')
app.secret_key = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
resend_lib.api_key = RESEND_API_KEY


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def query(sql, params=(), one=False, commit=False):
    db  = get_db()
    cur = db.cursor()
    cur.execute(sql, params)
    if commit:
        db.commit()
        cur.close()
        return None
    rows = cur.fetchone() if one else cur.fetchall()
    cur.close()
    return rows


# ── Auth helpers ──────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def staff_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('user_role') not in ('admin', 'staff'):
            flash('Access denied.')
            return redirect(url_for('partner_dashboard'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('user_role') != 'admin':
            flash('Admin access required.')
            return redirect(url_for('dashboard_admin'))
        return f(*args, **kwargs)
    return decorated


# ── Webhook engine ────────────────────────────────────────────────────────────

def fire_webhook(event_type, session_id, extra=None):
    if not WEBHOOK_URL:
        return
    try:
        event_id  = str(uuid.uuid4())
        session_row = query('SELECT * FROM test_sessions WHERE id=%s', (session_id,), one=True)
        if not session_row:
            return
        welder = query('SELECT * FROM welders WHERE id=%s', (session_row['welder_id'],), one=True)
        assignments = query(
            '''SELECT ta.*, w.wps_number, w.name as wps_name, w.process, w.position,
                      w.pipe_plate, w.size, w.thickness, cp.cpn_number
               FROM test_assignments ta
               LEFT JOIN wps w ON w.id = ta.wps_id
               LEFT JOIN coupon_parts cp ON cp.id = ta.cpn_id
               WHERE ta.session_id=%s''',
            (session_id,)
        )
        payload = {
            'event_id':        event_id,
            'event_type':      event_type,
            'timestamp':       datetime.utcnow().isoformat() + 'Z',
            'welder_id':       welder['picture_id'] if welder else None,
            'welder_name':     f"{welder['first_name']} {welder['last_name']}" if welder else None,
            'session_id':      session_row['session_number'],
            'check_in_time':   session_row['check_in_datetime'].isoformat() if session_row['check_in_datetime'] else None,
            'check_out_time':  session_row['check_out_datetime'].isoformat() if session_row['check_out_datetime'] else None,
            'status':          session_row['status'],
            'lab_number':      session_row['lab_number'],
            'tests': [
                {
                    'wps_number':  a['wps_number'],
                    'wps_name':    a['wps_name'],
                    'process':     a['process'],
                    'position':    a['position'],
                    'pipe_plate':  a['pipe_plate'],
                    'size':        a['size'],
                    'thickness':   a['thickness'],
                    'cpn_number':  a['cpn_number'],
                    'test_status': a['status'],
                }
                for a in (assignments or [])
            ],
        }
        if extra:
            payload.update(extra)

        body = json.dumps(payload).encode('utf-8')
        sig  = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()

        import urllib.request
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=body,
            headers={
                'Content-Type':        'application/json',
                'X-Maverick-Signature': f'sha256={sig}',
                'X-Event-Type':        event_type,
                'X-Event-Id':          event_id,
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status_code = resp.status
                resp_body   = resp.read(500).decode('utf-8', errors='replace')
        except Exception as e:
            status_code = 0
            resp_body   = str(e)

        query(
            '''INSERT INTO webhook_events
               (event_id, event_type, session_id, payload, endpoint_url, sent_at, response_code, response_body, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (event_id, event_type, session_id, json.dumps(payload), WEBHOOK_URL,
             datetime.utcnow(), status_code, resp_body,
             'sent' if 200 <= (status_code or 0) < 300 else 'failed'),
            commit=True
        )
    except Exception:
        traceback.print_exc(file=sys.stderr)


# ── Public: Homepage / Kiosk ──────────────────────────────────────────────────

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/checkin/tester', methods=['GET', 'POST'])
def checkin_tester():
    if request.method == 'POST':
        picture_id = request.form.get('picture_id', '').strip()
        if not picture_id:
            flash('Please enter your Picture ID number.')
            return redirect(url_for('checkin_tester'))
        welder = query('SELECT * FROM welders WHERE picture_id=%s AND active=TRUE',
                       (picture_id,), one=True)
        if not welder:
            flash('Picture ID not found. Please see the front desk.')
            return redirect(url_for('checkin_tester'))
        session['kiosk_welder_id']   = welder['id']
        session['kiosk_welder_name'] = f"{welder['first_name']} {welder['last_name']}"
        return redirect(url_for('safety_briefing', next='tester_confirmed'))
    return render_template('checkin_tester.html')

@app.route('/checkin/trainee', methods=['GET', 'POST'])
def checkin_trainee():
    if request.method == 'POST':
        name  = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        if not name:
            flash('Please enter your name.')
            return redirect(url_for('checkin_trainee'))
        session['kiosk_trainee_name']  = name
        session['kiosk_trainee_email'] = email
        return redirect(url_for('safety_briefing', next='trainee_confirmed'))
    return render_template('checkin_trainee.html')

@app.route('/checkin/visitor', methods=['GET', 'POST'])
def checkin_visitor():
    if request.method == 'POST':
        name    = request.form.get('name', '').strip()
        email   = request.form.get('email', '').strip()
        company = request.form.get('company', '').strip()
        visiting= request.form.get('visiting_person', '').strip()
        if not name:
            flash('Please enter your name.')
            return redirect(url_for('checkin_visitor'))
        query(
            'INSERT INTO visitors (name, email, company, visiting_person, check_in_datetime) VALUES (%s,%s,%s,%s,%s)',
            (name, email, company, visiting, datetime.utcnow()), commit=True
        )
        session['kiosk_visitor_name'] = name
        return redirect(url_for('safety_briefing', next='visitor_confirmed'))
    return render_template('checkin_visitor.html')

@app.route('/safety-briefing')
def safety_briefing():
    next_step = request.args.get('next', 'home')
    return render_template('safety_briefing.html', next_step=next_step)

@app.route('/checkin/confirmed/<kind>')
def checkin_confirmed(kind):
    name = (session.pop('kiosk_welder_name', None)
            or session.pop('kiosk_trainee_name', None)
            or session.pop('kiosk_visitor_name', None)
            or 'Guest')
    return render_template('checkin_confirmed.html', kind=kind, name=name)


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        pw    = request.form['password']
        user  = query('SELECT * FROM users WHERE email=%s AND active=TRUE', (email,), one=True)
        if user and check_password_hash(user['password_hash'], pw):
            session['user_id']    = user['id']
            session['user_name']  = user['name']
            session['user_role']  = user['role']
            session['client_id']  = user['client_id']
            if user['role'] == 'partner':
                return redirect(url_for('partner_dashboard'))
            return redirect(url_for('dashboard_admin'))
        flash('Invalid email or password.')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


# ── Partner portal ────────────────────────────────────────────────────────────

@app.route('/partner')
@login_required
def partner_dashboard():
    if session.get('user_role') != 'partner':
        return redirect(url_for('dashboard_admin'))
    client_id = session.get('client_id')
    sessions  = query(
        '''SELECT ts.*, w.first_name, w.last_name, w.picture_id
           FROM test_sessions ts
           JOIN welders w ON w.id = ts.welder_id
           WHERE ts.client_id=%s
           ORDER BY ts.check_in_datetime DESC LIMIT 100''',
        (client_id,)
    )
    return render_template('partner/dashboard.html', sessions=sessions)


# ── Staff dashboards ──────────────────────────────────────────────────────────

@app.route('/dashboard/admin')
@staff_required
def dashboard_admin():
    date_from = request.args.get('from', '')
    date_to   = request.args.get('to', '')
    visitors  = query(
        'SELECT * FROM visitors ORDER BY check_in_datetime DESC LIMIT 200'
    )
    return render_template('dashboard/admin.html', visitors=visitors,
                           date_from=date_from, date_to=date_to)

@app.route('/dashboard/shop')
@staff_required
def dashboard_shop():
    tab = request.args.get('tab', 'checked_in')
    if tab == 'checked_in':
        rows = query(
            '''SELECT ts.*, w.first_name, w.last_name, w.picture_id,
                      c.name as client_name
               FROM test_sessions ts
               JOIN welders w ON w.id = ts.welder_id
               LEFT JOIN clients c ON c.id = ts.client_id
               WHERE ts.status IN ('checked_in','testing')
               ORDER BY ts.check_in_datetime DESC'''
        )
    elif tab == 'checked_out':
        rows = query(
            '''SELECT ts.*, w.first_name, w.last_name, w.picture_id,
                      c.name as client_name
               FROM test_sessions ts
               JOIN welders w ON w.id = ts.welder_id
               LEFT JOIN clients c ON c.id = ts.client_id
               WHERE ts.status IN ('completed','cancelled')
                 AND ts.check_in_datetime::date = CURRENT_DATE
               ORDER BY ts.check_out_datetime DESC'''
        )
    else:
        rows = query(
            '''SELECT ts.*, w.first_name, w.last_name, w.picture_id,
                      c.name as client_name
               FROM test_sessions ts
               JOIN welders w ON w.id = ts.welder_id
               LEFT JOIN clients c ON c.id = ts.client_id
               ORDER BY ts.check_in_datetime DESC LIMIT 500'''
        )
    return render_template('dashboard/shop.html', rows=rows, tab=tab)

@app.route('/dashboard/lab')
@staff_required
def dashboard_lab():
    rows = query(
        '''SELECT ts.*, w.first_name, w.last_name, w.picture_id,
                  c.name as client_name,
                  json_agg(json_build_object(
                      'id', ta.id, 'wps_number', wp.wps_number, 'wps_name', wp.name,
                      'status', ta.status, 'vt_root', lr.vt_root, 'vt_cap', lr.vt_cap,
                      'lab_result', lr.lab_result, 'final_result', lr.final_result
                  ) ORDER BY ta.id) as tests
           FROM test_sessions ts
           JOIN welders w ON w.id = ts.welder_id
           LEFT JOIN clients c ON c.id = ts.client_id
           LEFT JOIN test_assignments ta ON ta.session_id = ts.id
           LEFT JOIN wps wp ON wp.id = ta.wps_id
           LEFT JOIN lab_results lr ON lr.test_assignment_id = ta.id
           WHERE ts.status IN ('checked_in','testing')
           GROUP BY ts.id, w.id, c.id
           ORDER BY ts.check_in_datetime DESC'''
    )
    return render_template('dashboard/lab.html', rows=rows)


# ── Test sessions ─────────────────────────────────────────────────────────────

@app.route('/sessions/new', methods=['GET', 'POST'])
@staff_required
def session_new():
    welders  = query('SELECT * FROM welders WHERE active=TRUE ORDER BY last_name, first_name')
    clients  = query('SELECT * FROM clients WHERE active=TRUE ORDER BY name')
    wps_list = query('SELECT * FROM wps WHERE active=TRUE ORDER BY wps_number')
    if request.method == 'POST':
        welder_id   = request.form.get('welder_id')
        client_id   = request.form.get('client_id')
        lab_number  = request.form.get('lab_number', '').strip()
        session_num = request.form.get('session_number', '').strip()
        po_number   = request.form.get('po_number', '').strip()
        job_number  = request.form.get('job_number', '').strip()
        sess_type   = request.form.get('type', 'WQ')
        if not welder_id or not lab_number:
            flash('Welder and Lab # are required.')
            return redirect(url_for('session_new'))
        now = datetime.utcnow()
        query(
            '''INSERT INTO test_sessions
               (session_number, lab_number, welder_id, client_id, type,
                po_number, job_number, check_in_datetime, status, added_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'checked_in',%s)''',
            (session_num or lab_number, lab_number, welder_id, client_id or None,
             sess_type, po_number, job_number, now, session['user_id']),
            commit=True
        )
        sess_row = query('SELECT id FROM test_sessions WHERE lab_number=%s ORDER BY id DESC LIMIT 1',
                         (lab_number,), one=True)
        if sess_row:
            wps_ids = request.form.getlist('wps_ids')
            for wps_id in wps_ids:
                cpn = query('SELECT id FROM coupon_parts WHERE wps_id=%s AND active=TRUE LIMIT 1',
                            (wps_id,), one=True)
                query(
                    'INSERT INTO test_assignments (session_id, wps_id, cpn_id, status) VALUES (%s,%s,%s,%s)',
                    (sess_row['id'], wps_id, cpn['id'] if cpn else None, 'pending'),
                    commit=True
                )
            fire_webhook('welder.checked_in', sess_row['id'])
        flash('Test session created.')
        return redirect(url_for('dashboard_shop'))
    return render_template('sessions/new.html',
                           welders=welders, clients=clients, wps_list=wps_list)

@app.route('/sessions/<int:sid>/status', methods=['POST'])
@staff_required
def session_status(sid):
    new_status = request.form.get('status')
    allowed    = ('checked_in', 'testing', 'completed', 'cancelled')
    if new_status not in allowed:
        flash('Invalid status.')
        return redirect(url_for('dashboard_shop'))
    updates = {'status': new_status}
    if new_status in ('completed', 'cancelled'):
        query('UPDATE test_sessions SET status=%s, check_out_datetime=%s WHERE id=%s',
              (new_status, datetime.utcnow(), sid), commit=True)
        event = 'welder.checked_out'
    else:
        query('UPDATE test_sessions SET status=%s WHERE id=%s', (new_status, sid), commit=True)
        event = 'test.started' if new_status == 'testing' else 'welder.checked_in'
    fire_webhook(event, sid)
    return redirect(request.referrer or url_for('dashboard_shop'))

@app.route('/sessions/<int:sid>/assignment/<int:aid>/result', methods=['POST'])
@staff_required
def record_result(sid, aid):
    vt_root     = request.form.get('vt_root', '')
    vt_cap      = request.form.get('vt_cap', '')
    lab_result  = request.form.get('lab_result', '')
    final       = request.form.get('final_result', '')
    test_status = request.form.get('test_status', 'completed')
    existing = query('SELECT id FROM lab_results WHERE test_assignment_id=%s', (aid,), one=True)
    if existing:
        query(
            '''UPDATE lab_results SET vt_root=%s, vt_cap=%s, lab_result=%s,
               final_result=%s, recorded_by=%s, recorded_at=%s WHERE id=%s''',
            (vt_root, vt_cap, lab_result, final, session['user_id'], datetime.utcnow(), existing['id']),
            commit=True
        )
    else:
        query(
            '''INSERT INTO lab_results
               (test_assignment_id, vt_root, vt_cap, lab_result, final_result, recorded_by, recorded_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s)''',
            (aid, vt_root, vt_cap, lab_result, final, session['user_id'], datetime.utcnow()),
            commit=True
        )
    query('UPDATE test_assignments SET status=%s WHERE id=%s', (test_status, aid), commit=True)
    event = 'test.completed' if test_status == 'completed' else 'test.cancelled'
    fire_webhook(event, sid)
    return redirect(url_for('dashboard_lab'))


# ── Welder Profiles ───────────────────────────────────────────────────────────

@app.route('/welders')
@staff_required
def welders_list():
    q    = request.args.get('q', '').strip()
    if q:
        rows = query(
            '''SELECT * FROM welders WHERE active=TRUE
               AND (first_name ILIKE %s OR last_name ILIKE %s OR picture_id ILIKE %s)
               ORDER BY last_name, first_name''',
            (f'%{q}%', f'%{q}%', f'%{q}%')
        )
    else:
        rows = query('SELECT * FROM welders WHERE active=TRUE ORDER BY last_name, first_name')
    return render_template('welders/list.html', welders=rows, q=q)

@app.route('/welders/new', methods=['GET', 'POST'])
@staff_required
def welder_new():
    if request.method == 'POST':
        picture_id = request.form.get('picture_id', '').strip()
        first      = request.form.get('first_name', '').strip()
        last       = request.form.get('last_name', '').strip()
        email      = request.form.get('email', '').strip()
        mobile     = request.form.get('mobile', '').strip()
        if not first or not last:
            flash('First and last name are required.')
            return redirect(url_for('welder_new'))
        query(
            'INSERT INTO welders (picture_id, first_name, last_name, email, mobile) VALUES (%s,%s,%s,%s,%s)',
            (picture_id or None, first, last, email or None, mobile or None), commit=True
        )
        flash(f'{first} {last} added.')
        return redirect(url_for('welders_list'))
    return render_template('welders/new.html')

@app.route('/welders/<int:wid>/edit', methods=['GET', 'POST'])
@staff_required
def welder_edit(wid):
    welder = query('SELECT * FROM welders WHERE id=%s', (wid,), one=True)
    if not welder:
        flash('Welder not found.')
        return redirect(url_for('welders_list'))
    if request.method == 'POST':
        query(
            '''UPDATE welders SET picture_id=%s, first_name=%s, last_name=%s,
               email=%s, mobile=%s WHERE id=%s''',
            (request.form.get('picture_id') or None,
             request.form.get('first_name', '').strip(),
             request.form.get('last_name', '').strip(),
             request.form.get('email') or None,
             request.form.get('mobile') or None, wid),
            commit=True
        )
        flash('Welder updated.')
        return redirect(url_for('welders_list'))
    return render_template('welders/edit.html', welder=welder)


# ── Invitations ───────────────────────────────────────────────────────────────

@app.route('/invitations')
@staff_required
def invitations_list():
    rows = query(
        '''SELECT i.*, c.name as client_name
           FROM invitations i LEFT JOIN clients c ON c.id = i.client_id
           ORDER BY i.expected_date DESC, i.id DESC LIMIT 200'''
    )
    return render_template('invitations/list.html', invitations=rows)

@app.route('/invitations/new', methods=['GET', 'POST'])
@staff_required
def invitation_new():
    clients = query('SELECT * FROM clients WHERE active=TRUE ORDER BY name')
    if request.method == 'POST':
        client_id    = request.form.get('client_id')
        expected     = request.form.get('expected_date')
        notes        = request.form.get('notes', '').strip()
        inv_num      = 'INV' + datetime.utcnow().strftime('%Y%m%d%H%M%S')
        query(
            'INSERT INTO invitations (invitation_number, client_id, expected_date, notes, created_by) VALUES (%s,%s,%s,%s,%s)',
            (inv_num, client_id or None, expected or None, notes or None, session['user_id']),
            commit=True
        )
        flash(f'Invitation {inv_num} created.')
        return redirect(url_for('invitations_list'))
    return render_template('invitations/new.html', clients=clients)


# ── Muster ────────────────────────────────────────────────────────────────────

@app.route('/muster')
@staff_required
def muster():
    zones = query('SELECT * FROM muster_zones WHERE active=TRUE ORDER BY name')
    data  = {}
    for z in zones:
        data[z['name']] = query(
            '''SELECT w.first_name, w.last_name, ts.id as session_id
               FROM muster_assignments ma
               JOIN test_sessions ts ON ts.id = ma.session_id
               JOIN welders w ON w.id = ts.welder_id
               WHERE ma.zone_id=%s AND ts.status IN ('checked_in','testing')''',
            (z['id'],)
        )
    return render_template('muster/index.html', zones=zones, data=data)

@app.route('/muster/assign', methods=['POST'])
@staff_required
def muster_assign():
    session_id = request.form.get('session_id')
    zone_id    = request.form.get('zone_id')
    query('DELETE FROM muster_assignments WHERE session_id=%s', (session_id,), commit=True)
    if zone_id:
        query('INSERT INTO muster_assignments (session_id, zone_id) VALUES (%s,%s)',
              (session_id, zone_id), commit=True)
    return redirect(url_for('muster'))


# ── Admin: Configurations ─────────────────────────────────────────────────────

@app.route('/admin/wps')
@staff_required
def admin_wps():
    q    = request.args.get('q', '').strip()
    rows = query(
        '''SELECT w.*, c.name as client_name FROM wps w
           LEFT JOIN clients c ON c.id = w.client_id
           WHERE w.active=TRUE
           ''' + ('AND (w.wps_number ILIKE %s OR w.name ILIKE %s)' if q else '') +
        ' ORDER BY w.wps_number',
        (f'%{q}%', f'%{q}%') if q else ()
    )
    return render_template('admin/wps.html', wps_list=rows, q=q)

@app.route('/admin/wps/new', methods=['GET', 'POST'])
@staff_required
def admin_wps_new():
    clients = query('SELECT * FROM clients WHERE active=TRUE ORDER BY name')
    if request.method == 'POST':
        query(
            '''INSERT INTO wps (wps_number, name, type, process, position, pipe_plate,
               size, thickness, filler_metal, price, cost_code, client_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (request.form.get('wps_number','').strip(),
             request.form.get('name','').strip(),
             request.form.get('type','WQ'),
             request.form.get('process','').strip(),
             request.form.get('position','').strip(),
             request.form.get('pipe_plate','').strip(),
             request.form.get('size','').strip(),
             request.form.get('thickness','').strip(),
             request.form.get('filler_metal','').strip(),
             request.form.get('price') or None,
             request.form.get('cost_code','').strip(),
             request.form.get('client_id') or None),
            commit=True
        )
        flash('WPS added.')
        return redirect(url_for('admin_wps'))
    return render_template('admin/wps_form.html', wps=None, clients=clients)

@app.route('/admin/wps/<int:wid>/edit', methods=['GET', 'POST'])
@staff_required
def admin_wps_edit(wid):
    wps     = query('SELECT * FROM wps WHERE id=%s', (wid,), one=True)
    clients = query('SELECT * FROM clients WHERE active=TRUE ORDER BY name')
    if request.method == 'POST':
        query(
            '''UPDATE wps SET wps_number=%s, name=%s, type=%s, process=%s, position=%s,
               pipe_plate=%s, size=%s, thickness=%s, filler_metal=%s, price=%s,
               cost_code=%s, client_id=%s WHERE id=%s''',
            (request.form.get('wps_number','').strip(),
             request.form.get('name','').strip(),
             request.form.get('type','WQ'),
             request.form.get('process','').strip(),
             request.form.get('position','').strip(),
             request.form.get('pipe_plate','').strip(),
             request.form.get('size','').strip(),
             request.form.get('thickness','').strip(),
             request.form.get('filler_metal','').strip(),
             request.form.get('price') or None,
             request.form.get('cost_code','').strip(),
             request.form.get('client_id') or None, wid),
            commit=True
        )
        flash('WPS updated.')
        return redirect(url_for('admin_wps'))
    return render_template('admin/wps_form.html', wps=wps, clients=clients)

@app.route('/admin/coupons')
@staff_required
def admin_coupons():
    q    = request.args.get('q', '').strip()
    rows = query(
        'SELECT cp.*, w.wps_number FROM coupon_parts cp LEFT JOIN wps w ON w.id=cp.wps_id'
        + (' WHERE cp.cpn_number ILIKE %s OR cp.description ILIKE %s' if q else ' WHERE cp.active=TRUE') +
        ' ORDER BY cp.cpn_number',
        (f'%{q}%', f'%{q}%') if q else ()
    )
    return render_template('admin/coupons.html', coupons=rows, q=q)

@app.route('/admin/coupons/new', methods=['GET', 'POST'])
@staff_required
def admin_coupon_new():
    wps_list = query('SELECT id, wps_number, name FROM wps WHERE active=TRUE ORDER BY wps_number')
    if request.method == 'POST':
        query(
            '''INSERT INTO coupon_parts (cpn_number, description, material_type, size, thickness, wps_id)
               VALUES (%s,%s,%s,%s,%s,%s)''',
            (request.form.get('cpn_number','').strip(),
             request.form.get('description','').strip(),
             request.form.get('material_type','').strip(),
             request.form.get('size','').strip(),
             request.form.get('thickness','').strip(),
             request.form.get('wps_id') or None),
            commit=True
        )
        flash('Coupon part added.')
        return redirect(url_for('admin_coupons'))
    return render_template('admin/coupon_form.html', coupon=None, wps_list=wps_list)

@app.route('/admin/coupons/<int:cid>/edit', methods=['GET', 'POST'])
@staff_required
def admin_coupon_edit(cid):
    coupon   = query('SELECT * FROM coupon_parts WHERE id=%s', (cid,), one=True)
    wps_list = query('SELECT id, wps_number, name FROM wps WHERE active=TRUE ORDER BY wps_number')
    if request.method == 'POST':
        query(
            '''UPDATE coupon_parts SET cpn_number=%s, description=%s, material_type=%s,
               size=%s, thickness=%s, wps_id=%s, active=%s WHERE id=%s''',
            (request.form.get('cpn_number','').strip(),
             request.form.get('description','').strip(),
             request.form.get('material_type','').strip(),
             request.form.get('size','').strip(),
             request.form.get('thickness','').strip(),
             request.form.get('wps_id') or None,
             request.form.get('active') == 'true', cid),
            commit=True
        )
        flash('Coupon part updated.')
        return redirect(url_for('admin_coupons'))
    return render_template('admin/coupon_form.html', coupon=coupon, wps_list=wps_list)

@app.route('/admin/clients')
@admin_required
def admin_clients():
    rows = query('SELECT * FROM clients ORDER BY name')
    return render_template('admin/clients.html', clients=rows)

@app.route('/admin/clients/new', methods=['GET', 'POST'])
@admin_required
def admin_client_new():
    if request.method == 'POST':
        query(
            'INSERT INTO clients (name, contact_name, contact_email, contact_phone) VALUES (%s,%s,%s,%s)',
            (request.form.get('name','').strip(),
             request.form.get('contact_name','').strip() or None,
             request.form.get('contact_email','').strip() or None,
             request.form.get('contact_phone','').strip() or None),
            commit=True
        )
        flash('Client added.')
        return redirect(url_for('admin_clients'))
    return render_template('admin/client_form.html', client=None)

@app.route('/admin/clients/<int:cid>/edit', methods=['GET', 'POST'])
@admin_required
def admin_client_edit(cid):
    client = query('SELECT * FROM clients WHERE id=%s', (cid,), one=True)
    if request.method == 'POST':
        query(
            '''UPDATE clients SET name=%s, contact_name=%s, contact_email=%s,
               contact_phone=%s, active=%s WHERE id=%s''',
            (request.form.get('name','').strip(),
             request.form.get('contact_name','').strip() or None,
             request.form.get('contact_email','').strip() or None,
             request.form.get('contact_phone','').strip() or None,
             request.form.get('active') == 'true', cid),
            commit=True
        )
        flash('Client updated.')
        return redirect(url_for('admin_clients'))
    return render_template('admin/client_form.html', client=client)

@app.route('/admin/users')
@admin_required
def admin_users():
    rows = query('SELECT u.*, c.name as client_name FROM users u LEFT JOIN clients c ON c.id=u.client_id ORDER BY u.name')
    return render_template('admin/users.html', users=rows)

@app.route('/admin/users/new', methods=['GET', 'POST'])
@admin_required
def admin_user_new():
    clients = query('SELECT * FROM clients WHERE active=TRUE ORDER BY name')
    if request.method == 'POST':
        name      = request.form.get('name','').strip()
        email     = request.form.get('email','').strip().lower()
        pw        = request.form.get('password','')
        role      = request.form.get('role','staff')
        client_id = request.form.get('client_id') or None
        if not name or not email or not pw:
            flash('Name, email, and password are required.')
            return redirect(url_for('admin_user_new'))
        query(
            'INSERT INTO users (name, email, password_hash, role, client_id) VALUES (%s,%s,%s,%s,%s)',
            (name, email, generate_password_hash(pw), role, client_id), commit=True
        )
        flash(f'User {name} created.')
        return redirect(url_for('admin_users'))
    return render_template('admin/user_form.html', user=None, clients=clients)

@app.route('/admin/users/<int:uid>/edit', methods=['GET', 'POST'])
@admin_required
def admin_user_edit(uid):
    user    = query('SELECT * FROM users WHERE id=%s', (uid,), one=True)
    clients = query('SELECT * FROM clients WHERE active=TRUE ORDER BY name')
    if request.method == 'POST':
        pw = request.form.get('password','').strip()
        if pw:
            query('UPDATE users SET password_hash=%s WHERE id=%s',
                  (generate_password_hash(pw), uid), commit=True)
        query(
            'UPDATE users SET name=%s, email=%s, role=%s, client_id=%s, active=%s WHERE id=%s',
            (request.form.get('name','').strip(),
             request.form.get('email','').strip().lower(),
             request.form.get('role','staff'),
             request.form.get('client_id') or None,
             request.form.get('active') == 'true', uid),
            commit=True
        )
        flash('User updated.')
        return redirect(url_for('admin_users'))
    return render_template('admin/user_form.html', user=user, clients=clients)


# ── Excel Import ──────────────────────────────────────────────────────────────

@app.route('/import', methods=['GET', 'POST'])
@staff_required
def import_excel():
    if request.method == 'POST':
        files = request.files.getlist('files')
        results, errors = [], []
        for f in files:
            if not f.filename:
                continue
            fname = secure_filename(f.filename)
            try:
                import openpyxl
                wb   = openpyxl.load_workbook(io.BytesIO(f.read()), data_only=True)
                ws   = wb.active
                rows = list(ws.iter_rows(values_only=True))

                # Parse header
                welder_name = None
                ss_number   = None
                lab_number  = None
                contractor  = None
                for row in rows[:10]:
                    r = [str(c).strip() if c is not None else '' for c in row]
                    if r[0] == 'Name' and r[1]:
                        welder_name = r[1]
                    if r[0] == 'S.S.#' and r[1]:
                        ss_number = r[1]
                    if r[0] == 'Lab#' or (len(r) > 8 and r[8] == 'Lab#'):
                        idx = r.index('Lab#') if 'Lab#' in r else 8
                        if idx + 1 < len(r) and r[idx + 1]:
                            lab_number = r[idx + 1]
                    if r[0] == 'Contractor' and r[1]:
                        contractor = r[1]

                if not welder_name:
                    errors.append(f'{fname}: could not parse welder name.')
                    continue

                # Find or create welder
                name_parts = welder_name.strip().split()
                last  = name_parts[-1] if len(name_parts) > 1 else welder_name
                first = ' '.join(name_parts[:-1]) if len(name_parts) > 1 else ''
                welder_row = query(
                    'SELECT * FROM welders WHERE picture_id=%s AND active=TRUE',
                    (ss_number,), one=True
                ) if ss_number else None
                if not welder_row:
                    welder_row = query(
                        'SELECT * FROM welders WHERE last_name ILIKE %s AND first_name ILIKE %s AND active=TRUE',
                        (last, first), one=True
                    )
                if not welder_row:
                    query(
                        'INSERT INTO welders (picture_id, first_name, last_name) VALUES (%s,%s,%s)',
                        (ss_number or None, first, last), commit=True
                    )
                    welder_row = query(
                        'SELECT * FROM welders WHERE last_name ILIKE %s AND first_name ILIKE %s ORDER BY id DESC LIMIT 1',
                        (last, first), one=True
                    )

                # Find client
                client_row = None
                if contractor:
                    client_row = query(
                        'SELECT * FROM clients WHERE name ILIKE %s AND active=TRUE LIMIT 1',
                        (f'%{contractor.split("/")[0].strip()}%',), one=True
                    )

                # Create test session
                if lab_number:
                    existing = query('SELECT id FROM test_sessions WHERE lab_number=%s', (lab_number,), one=True)
                    if existing:
                        errors.append(f'{fname}: session {lab_number} already exists — skipped.')
                        continue

                session_num = lab_number or ('IMP-' + datetime.utcnow().strftime('%Y%m%d%H%M%S'))
                query(
                    '''INSERT INTO test_sessions
                       (session_number, lab_number, welder_id, client_id, type,
                        check_in_datetime, status, added_by)
                       VALUES (%s,%s,%s,%s,'WQ',%s,'checked_in',%s)''',
                    (session_num, lab_number, welder_row['id'],
                     client_row['id'] if client_row else None,
                     datetime.utcnow(), session['user_id']),
                    commit=True
                )
                sess_row = query(
                    'SELECT id FROM test_sessions WHERE lab_number=%s ORDER BY id DESC LIMIT 1',
                    (lab_number,), one=True
                )

                # Parse test rows (column A = integer WPS number)
                test_count = 0
                for row in rows[8:]:
                    if row[0] is None or not str(row[0]).strip().isdigit():
                        continue
                    wps_num = str(int(row[0]))
                    wps_row = query('SELECT * FROM wps WHERE wps_number=%s LIMIT 1', (wps_num,), one=True)
                    if not wps_row:
                        continue
                    cpn = query('SELECT id FROM coupon_parts WHERE wps_id=%s AND active=TRUE LIMIT 1',
                                (wps_row['id'],), one=True)
                    query(
                        'INSERT INTO test_assignments (session_id, wps_id, cpn_id, status) VALUES (%s,%s,%s,%s)',
                        (sess_row['id'], wps_row['id'], cpn['id'] if cpn else None, 'pending'),
                        commit=True
                    )
                    test_count += 1

                fire_webhook('welder.checked_in', sess_row['id'])
                results.append(f'{fname}: imported {welder_name} — {test_count} test(s), Lab# {lab_number}')

            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                errors.append(f'{fname}: {e}')

        return render_template('import_result.html', results=results, errors=errors)
    return render_template('import.html')


# ── One-time setup (remove after first use) ───────────────────────────────────

@app.route('/setup/init-admin')
def setup_init_admin():
    token = request.args.get('t', '')
    if token != 'rci350-setup-2026':
        return 'Forbidden', 403
    existing = query('SELECT id FROM users WHERE email=%s', ('bleblanc@rcigroup.us',), one=True)
    if existing:
        return 'Admin user already exists.', 200
    query(
        'INSERT INTO users (name, email, password_hash, role) VALUES (%s,%s,%s,%s)',
        ('Brant LeBlanc', 'bleblanc@rcigroup.us',
         generate_password_hash('MavAdmin2026!'), 'admin'),
        commit=True
    )
    return 'Admin user created. Login: bleblanc@rcigroup.us / MavAdmin2026!', 200


# ── Visitors checkout ─────────────────────────────────────────────────────────

@app.route('/visitors/<int:vid>/checkout', methods=['POST'])
@staff_required
def visitor_checkout(vid):
    query('UPDATE visitors SET check_out_datetime=%s WHERE id=%s',
          (datetime.utcnow(), vid), commit=True)
    return redirect(url_for('dashboard_admin'))


if __name__ == '__main__':
    app.run(debug=True)
