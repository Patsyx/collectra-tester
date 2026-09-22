from __future__ import annotations
import json, os, sqlite3, mimetypes, secrets, socket, sys, traceback, shutil, tempfile
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from email.parser import BytesParser
from email.policy import default
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parent
PUBLIC=ROOT/'public'
DATA=ROOT/'data'
UPLOADS=DATA/'uploads'
REFERENCES=DATA/'references'
from services.db import init_db, connect, now_iso
from services.ai_service import AIService


def load_env():
    p=ROOT/'.env'
    if p.exists():
        for line in p.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.strip().startswith('#'):
                k,v=line.split('=',1); os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))
load_env()
ADMIN_KEY=os.getenv('COLLECTRA_ADMIN_KEY','collectra-demo')
DEMO_ONLY=True  # hardcoded: this deployment ALWAYS shows the seller-only demo, no env var needed
MAX_UPLOAD_BYTES=int(os.getenv('COLLECTRA_MAX_UPLOAD_MB','150'))*1024*1024
AI=AIService(ROOT)


def json_bytes(obj): return json.dumps(obj,ensure_ascii=False).encode('utf-8')

def safe_filename(name:str)->str:
    name=Path(name or 'upload.bin').name
    base=''.join(c for c in name if c.isalnum() or c in '._-')[:80]
    return f"{secrets.token_hex(7)}_{base or 'upload.bin'}"

def save_upload(filename:str, data:bytes, folder=UPLOADS):
    folder.mkdir(parents=True,exist_ok=True)
    fn=safe_filename(filename); path=folder/fn; path.write_bytes(data)
    web_prefix='/references/' if folder==REFERENCES else '/uploads/'
    return path, web_prefix+fn

def parse_multipart(handler):
    ctype=handler.headers.get('Content-Type','')
    length=int(handler.headers.get('Content-Length','0') or 0)
    body=handler.rfile.read(length)
    raw=(f'Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n').encode()+body
    msg=BytesParser(policy=default).parsebytes(raw)
    fields={}; files={}
    if not msg.is_multipart(): return fields, files
    for part in msg.iter_parts():
        cd=part.get('Content-Disposition','')
        params=dict(part.get_params(header='content-disposition',failobj=[]))
        name=params.get('name'); filename=params.get('filename')
        if not name: continue
        payload=part.get_payload(decode=True) or b''
        if filename: files[name]=(filename,payload,part.get_content_type())
        else: fields[name]=payload.decode(part.get_content_charset() or 'utf-8',errors='replace')
    return fields,files

def store_with_rating(row, con):
    d=dict(row)
    stats=con.execute('SELECT ROUND(AVG(rating),1) rating, COUNT(*) review_count FROM reviews WHERE store_id=?',(row['id'],)).fetchone()
    d['rating']=stats['rating'] or 0; d['review_count']=stats['review_count'] or 0
    return d

def public_store(d):
    """Strip the private seller token before sending a store to a public (non-admin) endpoint."""
    d=dict(d); d.pop('token',None); return d

def product_with_store(row, con):
    d=dict(row)
    st=con.execute('SELECT * FROM stores WHERE id=?',(row['store_id'],)).fetchone()
    if st:
        sd=store_with_rating(st,con)
        d['store_name']=sd['name']; d['store_verified']=bool(sd['verified']); d['store_rating']=sd['rating']
    d['verified']=bool(d['verified'])
    return d

class Handler(BaseHTTPRequestHandler):
    server_version='Collectra/1.0'
    def log_message(self, fmt,*args): print('[Collectra]',fmt%args)
    def send_json(self,obj,status=200):
        b=json_bytes(obj); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(b)
    def read_json(self):
        length=int(self.headers.get('Content-Length','0') or 0)
        raw=self.rfile.read(length) if length else b'{}'
        return json.loads(raw.decode('utf-8') or '{}')
    def admin_ok(self): return self.headers.get('X-Admin-Key','')==ADMIN_KEY
    def do_GET(self):
        try:
            p=urlparse(self.path); path=unquote(p.path); q=parse_qs(p.query)
            if path.startswith('/api/'): return self.api_get(path,q)
            return self.serve_static(path)
        except Exception as e:
            traceback.print_exc(); self.send_json({'error':str(e)},500)
    def do_POST(self):
        try:
            length=int(self.headers.get('Content-Length','0') or 0)
            if length > MAX_UPLOAD_BYTES:
                return self.send_json({'error':f'Upload is too large. Keep each file under {MAX_UPLOAD_BYTES//1024//1024} MB.'},413)
            p=urlparse(self.path); path=p.path
            if path.startswith('/api/'): return self.api_post(path)
            self.send_json({'error':'Not found'},404)
        except Exception as e:
            traceback.print_exc(); self.send_json({'error':str(e)},500)
    def serve_static(self,path):
        if path.startswith('/uploads/'):
            file=UPLOADS/path[len('/uploads/'):]
        elif path.startswith('/references/'):
            file=REFERENCES/path[len('/references/'):]
        else:
            rel='index.html' if path in ('','/') else path.lstrip('/')
            file=PUBLIC/rel
            if not file.exists() or file.is_dir(): file=PUBLIC/'index.html'
        try:
            data=file.read_bytes()
        except FileNotFoundError:
            return self.send_error(404)
        if file.name=='index.html':
            flag=b'<script>window.COLLECTRA_DEMO_ONLY='+(b'true' if DEMO_ONLY else b'false')+b';</script>'
            data=data.replace(b'</head>', flag+b'</head>', 1)
        mime=mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
        self.send_response(200); self.send_header('Content-Type',mime); self.send_header('Content-Length',str(len(data)))
        if file.name=='sw.js': self.send_header('Service-Worker-Allowed','/')
        self.end_headers(); self.wfile.write(data)
    def api_get(self,path,q):
        con=connect()
        try:
            if path=='/api/health': return self.send_json({'ok':True,'ai_provider':AI.provider_name})
            if path=='/api/stores':
                rows=con.execute('SELECT * FROM stores ORDER BY verified DESC, id DESC').fetchall(); return self.send_json([public_store(store_with_rating(r,con)) for r in rows])
            if path=='/api/store':
                sid=int(q.get('id',['0'])[0]); st=con.execute('SELECT * FROM stores WHERE id=?',(sid,)).fetchone()
                if not st: return self.send_json({'error':'Store not found'},404)
                data=public_store(store_with_rating(st,con)); data['reviews']=[dict(r) for r in con.execute('SELECT * FROM reviews WHERE store_id=? ORDER BY id DESC',(sid,)).fetchall()]
                data['products']=[product_with_store(r,con) for r in con.execute('SELECT * FROM products WHERE store_id=? ORDER BY id DESC',(sid,)).fetchall()]
                return self.send_json(data)
            if path=='/api/products':
                rows=con.execute('SELECT * FROM products WHERE stock>0 ORDER BY id ASC').fetchall(); return self.send_json([product_with_store(r,con) for r in rows])
            if path=='/api/favorites':
                device=q.get('device_id',[''])[0]
                rows=con.execute('''SELECT p.* FROM products p JOIN favorites f ON f.product_id=p.id WHERE f.device_id=? ORDER BY f.created_at DESC''',(device,)).fetchall()
                return self.send_json([product_with_store(r,con) for r in rows])
            if path=='/api/addresses':
                device=q.get('device_id',[''])[0]; return self.send_json([dict(r) for r in con.execute('SELECT * FROM addresses WHERE device_id=? ORDER BY is_default DESC,id DESC',(device,)).fetchall()])
            if path=='/api/orders':
                device=q.get('device_id',[''])[0]; out=[]
                for r in con.execute('SELECT * FROM orders WHERE device_id=? ORDER BY id DESC',(device,)).fetchall():
                    d=dict(r); d['items']=[dict(x) for x in con.execute('SELECT * FROM order_items WHERE order_id=?',(r['id'],)).fetchall()]; out.append(d)
                return self.send_json(out)
            if path=='/api/demo-seller-info':
                st=con.execute("SELECT * FROM stores WHERE name='Collectra Seller Demo Store'").fetchone()
                if not st: return self.send_json({'error':'Demo store not seeded'},404)
                orow=con.execute("SELECT id FROM orders WHERE device_id='demo-seller-buyer' ORDER BY id DESC LIMIT 1").fetchone()
                return self.send_json({'store_token':st['token'],'store_name':st['name'],'order_id':orow['id'] if orow else None})
            if path=='/api/seller-login':
                token=q.get('store_token',[''])[0]
                st=con.execute('SELECT * FROM stores WHERE token=?',(token,)).fetchone()
                if not st: return self.send_json({'error':'Invalid store token'},401)
                return self.send_json({'ok':True,'store':public_store(store_with_rating(st,con))})
            if path=='/api/seller-orders':
                token=q.get('store_token',[''])[0]
                st=con.execute('SELECT * FROM stores WHERE token=?',(token,)).fetchone()
                if not st: return self.send_json({'error':'Invalid store token'},401)
                rows=con.execute('''SELECT DISTINCT o.* FROM orders o
                    JOIN order_items oi ON oi.order_id=o.id
                    JOIN products p ON p.id=oi.product_id
                    WHERE p.store_id=? ORDER BY o.id DESC''',(st['id'],)).fetchall()
                out=[]
                for r in rows:
                    d=dict(r)
                    d['items']=[dict(x) for x in con.execute('''SELECT oi.* FROM order_items oi
                        JOIN products p ON p.id=oi.product_id
                        WHERE oi.order_id=? AND p.store_id=?''',(r['id'],st['id'])).fetchall()]
                    out.append(d)
                return self.send_json({'store_name':st['name'],'orders':out})
            if path=='/api/admin/dashboard':
                if not self.admin_ok(): return self.send_json({'error':'Unauthorized'},401)
                apps=[dict(r) for r in con.execute('SELECT * FROM seller_applications ORDER BY id DESC').fetchall()]
                tickets=[dict(r) for r in con.execute('SELECT * FROM support_tickets ORDER BY id DESC LIMIT 50').fetchall()]
                verifs=[dict(r) for r in con.execute('SELECT * FROM verification_results ORDER BY id DESC LIMIT 50').fetchall()]
                stores=[store_with_rating(r,con) for r in con.execute('SELECT * FROM stores ORDER BY id DESC').fetchall()]
                refs=[dict(r) for r in con.execute('SELECT * FROM reference_cards ORDER BY id DESC LIMIT 50').fetchall()]
                demo_feedback=[dict(r) for r in con.execute('SELECT * FROM demo_feedback ORDER BY id DESC LIMIT 100').fetchall()]
                return self.send_json({'applications':apps,'tickets':tickets,'verifications':verifs,'stores':stores,'references':refs,'demo_feedback':demo_feedback})
            return self.send_json({'error':'Unknown endpoint'},404)
        finally: con.close()
    def api_post(self,path):
        con=connect()
        try:
            if path=='/api/favorites':
                d=self.read_json(); device=d['device_id']; pid=int(d['product_id'])
                exists=con.execute('SELECT 1 FROM favorites WHERE device_id=? AND product_id=?',(device,pid)).fetchone()
                if exists: con.execute('DELETE FROM favorites WHERE device_id=? AND product_id=?',(device,pid)); active=False
                else: con.execute('INSERT INTO favorites VALUES(?,?,?)',(device,pid,now_iso())); active=True
                con.commit(); return self.send_json({'active':active})
            if path=='/api/reviews':
                d=self.read_json(); rating=max(1,min(5,int(d['rating'])))
                con.execute('INSERT INTO reviews(store_id,device_id,rating,text,created_at) VALUES(?,?,?,?,?)',(int(d['store_id']),d['device_id'],rating,d.get('text','')[:800],now_iso())); con.commit(); return self.send_json({'ok':True})
            if path=='/api/seller-applications':
                d=self.read_json(); con.execute('INSERT INTO seller_applications(email,shop_name,social_link,status,created_at) VALUES(?,?,?,?,?)',(d['email'],d.get('shop_name',''),d.get('social_link',''),'pending',now_iso())); con.commit(); return self.send_json({'ok':True,'message':"Thanks — we'll review your seller request and get back to you soon."})
            if path=='/api/demo-feedback':
                d=self.read_json(); rating=max(1,min(5,int(d.get('rating',0) or 0)))
                con.execute('INSERT INTO demo_feedback(role,rating,liked,improve,device_id,created_at) VALUES(?,?,?,?,?,?)',
                    (d.get('role','seller')[:50],rating,d.get('liked','')[:1000],d.get('improve','')[:1000],d.get('device_id','')[:100],now_iso()))
                con.commit(); return self.send_json({'ok':True,'message':'Thanks for testing — your feedback was recorded.'})
            if path=='/api/addresses':
                d=self.read_json(); device=d['device_id']; is_default=1 if d.get('is_default',True) else 0
                if is_default: con.execute('UPDATE addresses SET is_default=0 WHERE device_id=?',(device,))
                con.execute('''INSERT INTO addresses(device_id,label,name,phone,line1,district,city,province,postal,is_default,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (device,d['label'],d['name'],d['phone'],d['line1'],d.get('district',''),d['city'],d['province'],d['postal'],is_default,now_iso()))
                con.commit(); return self.send_json({'ok':True,'id':con.execute('SELECT last_insert_rowid() id').fetchone()['id']})
            if path=='/api/orders':
                d=self.read_json(); items=d.get('items',[]); device=d['device_id']; address_id=d.get('address_id')
                if not items: return self.send_json({'error':'Cart is empty'},400)
                total=0; resolved=[]
                con.execute('BEGIN IMMEDIATE')
                for item in items:
                    pid=int(item['product_id']); qty=max(1,int(item.get('qty',1))); p=con.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
                    if not p or p['stock']<qty: con.rollback(); return self.send_json({'error':f'Not enough stock for product {pid}'},409)
                    total+=p['price']*qty; resolved.append((p,qty)); con.execute('UPDATE products SET stock=stock-? WHERE id=?',(qty,pid))
                con.execute('INSERT INTO orders(device_id,total,address_id,status,payment_status,created_at) VALUES(?,?,?,?,?,?)',(device,total,address_id,'Awaiting Payment','pending',now_iso())); oid=con.execute('SELECT last_insert_rowid() id').fetchone()['id']
                for p,qty in resolved: con.execute('INSERT INTO order_items(order_id,product_id,title,qty,unit_price) VALUES(?,?,?,?,?)',(oid,p['id'],p['title'],qty,p['price']))
                con.commit(); return self.send_json({'ok':True,'order_id':oid,'total':round(total,2)})
            if path=='/api/evidence-step-check':
                fields,files=parse_multipart(self)
                if not files: return self.send_json({'error':'Frame image required'},400)
                key=next(iter(files)); fn,blob,mime=files[key]
                if not mime.startswith('image/'): return self.send_json({'error':'Frame must be an image'},400)
                step_text=fields.get('step_text','')[:300]; kind=fields.get('kind','packing')
                result=AI.check_step_frame(blob,mime,step_text,kind)
                return self.send_json(result)
            if path in ('/api/payment-slip','/api/order-evidence'):
                fields,files=parse_multipart(self)
                if not files: return self.send_json({'error':'File required'},400)
                key=next(iter(files)); fn,blob,mime=files[key]
                oid=int(fields.get('order_id','0'))
                if not con.execute('SELECT 1 FROM orders WHERE id=?',(oid,)).fetchone():
                    return self.send_json({'error':'Order not found'},404)
                ai_result=None
                if path=='/api/payment-slip':
                    if not mime.startswith('image/'):
                        return self.send_json({'error':'Payment slip must be an image'},400)
                    saved_path,url=save_upload(fn,blob)
                    ai_result=AI.verify_slip(saved_path)
                    con.execute('''UPDATE orders SET slip_url=?,payment_status=?,
                        slip_ai_status=?,slip_ai_confidence=?,slip_ai_summary=?,slip_ai_json=? WHERE id=?''',
                        (url,'slip_uploaded',ai_result.get('status',''),ai_result.get('confidence',0),
                         ai_result.get('summary',''),json.dumps(ai_result),oid))
                else:
                    if not mime.startswith('video/'):
                        return self.send_json({'error':'Packing and unboxing evidence must be a video'},400)
                    kind=fields.get('kind','packing')
                    if kind not in ('packing','unboxing'):
                        return self.send_json({'error':'Evidence kind must be packing or unboxing'},400)
                    if kind=='packing':
                        store_token=fields.get('store_token','')
                        st=con.execute('SELECT * FROM stores WHERE token=?',(store_token,)).fetchone()
                        if not st: return self.send_json({'error':'Invalid store token'},401)
                        owns=con.execute('''SELECT 1 FROM order_items oi JOIN products p ON p.id=oi.product_id
                            WHERE oi.order_id=? AND p.store_id=?''',(oid,st['id'])).fetchone()
                        if not owns: return self.send_json({'error':'This store has no items in that order'},403)
                    else:
                        device=fields.get('device_id','')
                        orow=con.execute('SELECT device_id FROM orders WHERE id=?',(oid,)).fetchone()
                        if not orow or orow['device_id']!=device:
                            return self.send_json({'error':'This order does not belong to this device'},403)
                    guide_steps=fields.get('guide_steps','[]')[:5000]
                    try:
                        parsed_steps=json.loads(guide_steps)
                        if not isinstance(parsed_steps,list) or len(parsed_steps)<5:
                            return self.send_json({'error':'Complete all guided video steps before upload'},400)
                    except Exception:
                        return self.send_json({'error':'Invalid guide-step data'},400)
                    saved_path,url=save_upload(fn,blob)
                    if kind=='packing':
                        ai_result=AI.verify_packing_video(saved_path,parsed_steps)
                        con.execute('''UPDATE orders SET packing_video_url=?,packing_steps_json=?,packing_evidence_status=?,
                            packing_ai_status=?,packing_ai_confidence=?,packing_ai_summary=?,packing_ai_json=? WHERE id=?''',
                            (url,guide_steps,'uploaded',ai_result.get('status',''),ai_result.get('confidence',0),
                             ai_result.get('summary',''),json.dumps(ai_result),oid))
                    else:
                        prow=con.execute('SELECT packing_video_url FROM orders WHERE id=?',(oid,)).fetchone()
                        packing_path=None
                        if prow and prow['packing_video_url']:
                            candidate=UPLOADS/Path(prow['packing_video_url']).name
                            if candidate.exists(): packing_path=candidate
                        ai_result=AI.verify_unboxing_video(saved_path,parsed_steps,packing_path)
                        match_status='no_packing_video' if not packing_path else ai_result.get('status','Needs Review')
                        if packing_path and ai_result.get('matches_packing_card') is False:
                            match_status='Mismatch'
                        con.execute('''UPDATE orders SET unboxing_video_url=?,unboxing_steps_json=?,unboxing_evidence_status=?,
                            unboxing_ai_status=?,unboxing_ai_confidence=?,unboxing_ai_summary=?,unboxing_ai_json=?,evidence_match_status=? WHERE id=?''',
                            (url,guide_steps,'uploaded',ai_result.get('status',''),ai_result.get('confidence',0),
                             ai_result.get('summary',''),json.dumps(ai_result),match_status,oid))
                con.commit()
                resp={'ok':True,'url':url}
                if ai_result is not None: resp['ai']=ai_result
                return self.send_json(resp)
            if path=='/api/support-ticket':
                d=self.read_json(); con.execute('INSERT INTO support_tickets(device_id,email,message,status,created_at) VALUES(?,?,?,?,?)',(d['device_id'],d.get('email',''),d.get('message','')[:1500],'open',now_iso())); con.commit(); return self.send_json({'ok':True,'message':'A human admin request has been created.'})
            if path=='/api/chat':
                d=self.read_json(); return self.send_json({'reply':AI.chat(d.get('message',''))})
            if path=='/api/products':
                fields,files=parse_multipart(self); token=fields.get('store_token',''); st=con.execute('SELECT * FROM stores WHERE token=?',(token,)).fetchone()
                if not st: return self.send_json({'error':'Invalid store token'},401)
                image_url=''
                if 'image' in files:
                    fn,blob,_=files['image']; _,image_url=save_upload(fn,blob)
                con.execute('''INSERT INTO products(store_id,title,category,price,stock,condition,image_url,verified,description,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (st['id'],fields.get('title','Untitled'),fields.get('category','Other'),float(fields.get('price','0')),int(fields.get('stock','1')),fields.get('condition','Like New'),image_url,int(st['verified']),fields.get('description','')[:1200],now_iso()))
                con.commit(); return self.send_json({'ok':True,'product_id':con.execute('SELECT last_insert_rowid() id').fetchone()['id']})
            if path=='/api/verify':
                fields,files=parse_multipart(self)
                required=('front','back','edges','surface')
                missing=[key for key in required if key not in files]
                if missing:
                    labels={'front':'front','back':'back','edges':'edges/corners detail','surface':'surface/logo/foil detail'}
                    return self.send_json({'error':'Required photo missing: '+', '.join(labels[x] for x in missing)},400)
                saved={}
                for key in required:
                    fn,blob,mime=files[key]
                    if not mime.startswith('image/'):
                        return self.send_json({'error':f'{key} must be an image'},400)
                    saved[key]=save_upload(fn,blob)
                category=fields.get('category',''); card_name=fields.get('card_name',''); device=fields.get('device_id','')
                refs=[]
                query='SELECT * FROM reference_cards WHERE 1=1'; params=[]
                if category: query+=' AND category=?'; params.append(category)
                if card_name: query+=' AND (card_name LIKE ? OR set_name LIKE ?)'; params += [f'%{card_name}%',f'%{card_name}%']
                query+=' ORDER BY id DESC LIMIT 3'
                for r in con.execute(query,params).fetchall():
                    p=REFERENCES/Path(r['image_url']).name
                    if p.exists(): refs.append(p)
                result=AI.verify(saved['front'][0],saved['back'][0],saved['edges'][0],saved['surface'][0],category,card_name,refs)
                con.execute('''INSERT INTO verification_results(device_id,category,card_name,verdict,confidence,summary,details_json,front_url,back_url,edges_url,surface_url,provider,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (device,category,card_name,result['verdict'],result['confidence'],result['summary'],json.dumps(result['details']),saved['front'][1],saved['back'][1],saved['edges'][1],saved['surface'][1],result['provider'],now_iso()))
                con.commit(); return self.send_json(result)
            if path=='/api/admin/approve-seller':
                if not self.admin_ok(): return self.send_json({'error':'Unauthorized'},401)
                d=self.read_json(); aid=int(d['application_id']); app=con.execute('SELECT * FROM seller_applications WHERE id=?',(aid,)).fetchone()
                if not app: return self.send_json({'error':'Application not found'},404)
                token=secrets.token_urlsafe(18); name=d.get('store_name') or app['shop_name'] or app['email'].split('@')[0]
                con.execute('INSERT INTO stores(name,description,logo_url,verified,category,token,created_at) VALUES(?,?,?,?,?,?,?)',(name,'Approved Collectra seller','',1,d.get('category','Mixed'),token,now_iso())); con.execute('UPDATE seller_applications SET status=? WHERE id=?',('approved',aid)); con.commit(); return self.send_json({'ok':True,'store_token':token,'store_name':name})
            if path=='/api/admin/create-store':
                if not self.admin_ok(): return self.send_json({'error':'Unauthorized'},401)
                d=self.read_json(); token=secrets.token_urlsafe(18); con.execute('INSERT INTO stores(name,description,logo_url,verified,category,token,created_at) VALUES(?,?,?,?,?,?,?)',(d['name'],d.get('description',''),d.get('logo_url',''),1 if d.get('verified') else 0,d.get('category','Mixed'),token,now_iso())); con.commit(); return self.send_json({'ok':True,'store_token':token})
            if path=='/api/admin/reference':
                if not self.admin_ok(): return self.send_json({'error':'Unauthorized'},401)
                fields,files=parse_multipart(self)
                if 'image' not in files: return self.send_json({'error':'Image required'},400)
                fn,blob,_=files['image']; _,url=save_upload(fn,blob,REFERENCES)
                con.execute('INSERT INTO reference_cards(category,card_name,set_name,image_url,notes,created_at) VALUES(?,?,?,?,?,?)',(fields.get('category','Other'),fields.get('card_name',''),fields.get('set_name',''),url,fields.get('notes',''),now_iso())); con.commit(); return self.send_json({'ok':True,'url':url})
            return self.send_json({'error':'Unknown endpoint'},404)
        finally: con.close()

def get_lan_ip():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); ip=s.getsockname()[0]; s.close(); return ip
    except Exception: return '127.0.0.1'

if __name__=='__main__':
    init_db(); port=int(os.getenv('PORT','8000')); host='0.0.0.0'
    print('\nCollectra is running.')
    print(f'Computer: http://localhost:{port}')
    print(f'Phone on same Wi-Fi: http://{get_lan_ip()}:{port}')
    print('Admin prototype: open Profile → Admin, default key: collectra-demo (change in .env)')
    print('Press Ctrl+C to stop.\n')
    ThreadingHTTPServer((host,port),Handler).serve_forever()
