from __future__ import annotations
import sqlite3, secrets
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'data'
DB_PATH = DATA_DIR / 'collectra.db'
UPLOADS = DATA_DIR / 'uploads'
REFERENCES = DATA_DIR / 'references'


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect():
    DATA_DIR.mkdir(exist_ok=True)
    UPLOADS.mkdir(exist_ok=True)
    REFERENCES.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    return con


def _ensure_column(cur, table: str, column: str, definition: str):
    cols = {r[1] for r in cur.execute(f'PRAGMA table_info({table})').fetchall()}
    if column not in cols:
        cur.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


def init_db():
    con = connect()
    cur = con.cursor()
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS stores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        logo_url TEXT DEFAULT '',
        verified INTEGER DEFAULT 0,
        category TEXT DEFAULT 'Mixed',
        token TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        category TEXT NOT NULL,
        price REAL NOT NULL,
        stock INTEGER NOT NULL DEFAULT 1,
        condition TEXT DEFAULT 'Like New',
        image_url TEXT DEFAULT '',
        verified INTEGER DEFAULT 0,
        description TEXT DEFAULT '',
        ai_status TEXT DEFAULT '', ai_confidence INTEGER DEFAULT 0, ai_summary TEXT DEFAULT '', ai_json TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(store_id) REFERENCES stores(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id INTEGER NOT NULL,
        device_id TEXT NOT NULL,
        rating INTEGER NOT NULL,
        text TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(store_id) REFERENCES stores(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS favorites (
        device_id TEXT NOT NULL,
        product_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(device_id, product_id),
        FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS seller_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        shop_name TEXT DEFAULT '',
        social_link TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS addresses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        label TEXT NOT NULL,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        line1 TEXT NOT NULL,
        district TEXT DEFAULT '',
        city TEXT NOT NULL,
        province TEXT NOT NULL,
        postal TEXT NOT NULL,
        is_default INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        total REAL NOT NULL,
        address_id INTEGER,
        status TEXT DEFAULT 'Awaiting Payment',
        payment_status TEXT DEFAULT 'pending',
        slip_url TEXT DEFAULT '',
        tracking_number TEXT DEFAULT '',
        packing_video_url TEXT DEFAULT '',
        unboxing_video_url TEXT DEFAULT '',
        packing_steps_json TEXT DEFAULT '[]',
        unboxing_steps_json TEXT DEFAULT '[]',
        packing_evidence_status TEXT DEFAULT 'not_uploaded',
        unboxing_evidence_status TEXT DEFAULT 'not_uploaded',
        created_at TEXT NOT NULL,
        FOREIGN KEY(address_id) REFERENCES addresses(id)
    );
    CREATE TABLE IF NOT EXISTS order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        qty INTEGER NOT NULL,
        unit_price REAL NOT NULL,
        FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS support_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        email TEXT DEFAULT '',
        message TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS verification_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        category TEXT DEFAULT '',
        card_name TEXT DEFAULT '',
        verdict TEXT NOT NULL,
        confidence INTEGER NOT NULL,
        summary TEXT DEFAULT '',
        details_json TEXT DEFAULT '{}',
        front_url TEXT DEFAULT '',
        back_url TEXT DEFAULT '',
        edges_url TEXT DEFAULT '',
        surface_url TEXT DEFAULT '',
        provider TEXT DEFAULT 'local',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS reference_cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        card_name TEXT DEFAULT '',
        set_name TEXT DEFAULT '',
        image_url TEXT NOT NULL,
        notes TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS demo_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT DEFAULT 'seller',
        rating INTEGER DEFAULT 0,
        liked TEXT DEFAULT '',
        improve TEXT DEFAULT '',
        device_id TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );
    ''')

    # Lightweight migrations so this build can also replace an older prototype folder.
    for col, definition in [
        ('packing_steps_json', "TEXT DEFAULT '[]'"),
        ('unboxing_steps_json', "TEXT DEFAULT '[]'"),
        ('packing_evidence_status', "TEXT DEFAULT 'not_uploaded'"),
        ('unboxing_evidence_status', "TEXT DEFAULT 'not_uploaded'"),
        ('packing_ai_status', "TEXT DEFAULT ''"),
        ('packing_ai_confidence', "INTEGER DEFAULT 0"),
        ('packing_ai_summary', "TEXT DEFAULT ''"),
        ('packing_ai_json', "TEXT DEFAULT '{}'"),
        ('unboxing_ai_status', "TEXT DEFAULT ''"),
        ('unboxing_ai_confidence', "INTEGER DEFAULT 0"),
        ('unboxing_ai_summary', "TEXT DEFAULT ''"),
        ('unboxing_ai_json', "TEXT DEFAULT '{}'"),
        ('evidence_match_status', "TEXT DEFAULT ''"),
        ('slip_ai_status', "TEXT DEFAULT ''"),
        ('slip_ai_confidence', "INTEGER DEFAULT 0"),
        ('slip_ai_summary', "TEXT DEFAULT ''"),
        ('slip_ai_json', "TEXT DEFAULT '{}'"),
    ]:
        _ensure_column(cur, 'orders', col, definition)
    for col, definition in [
        ('ai_status', "TEXT DEFAULT ''"),
        ('ai_confidence', "INTEGER DEFAULT 0"),
        ('ai_summary', "TEXT DEFAULT ''"),
        ('ai_json', "TEXT DEFAULT '{}'"),
    ]:
        _ensure_column(cur, 'products', col, definition)
    for col, definition in [
        ('edges_url', "TEXT DEFAULT ''"),
        ('surface_url', "TEXT DEFAULT ''"),
    ]:
        _ensure_column(cur, 'verification_results', col, definition)

    count = cur.execute('SELECT COUNT(*) AS c FROM stores').fetchone()['c']
    if count == 0:
        seed_initial_stock(cur)
    con.commit()
    con.close()


def seed_initial_stock(cur):
    stores = [
        (
            'Collectra T-pop Stock',
            'Initial T-pop inventory supplied for Collectra prototype testing.',
            '/assets/collectra-logo.png',
            1,
            'T-pop',
        ),
        (
            'Collectra K-pop Stock',
            'Initial K-pop inventory supplied for Collectra prototype testing.',
            '/assets/collectra-logo.png',
            1,
            'K-pop',
        ),
    ]
    ids = []
    for name, desc, logo, verified, cat in stores:
        token = secrets.token_urlsafe(16)
        cur.execute(
            'INSERT INTO stores(name,description,logo_url,verified,category,token,created_at) VALUES(?,?,?,?,?,?,?)',
            (name, desc, logo, verified, cat, token, now_iso()),
        )
        ids.append(cur.lastrowid)

    # Product order follows the 10 photos supplied by the project team.
    products = [
        (ids[0], 'T-pop Photocard 01', 'T-pop', 40, 1, 'Like New', '/assets/initial-stock-01.webp', 1),
        (ids[0], 'T-pop Photocard 02', 'T-pop', 90, 1, 'Like New', '/assets/initial-stock-02.webp', 1),
        (ids[0], 'T-pop Photocard 03', 'T-pop', 30, 1, 'Like New', '/assets/initial-stock-03.webp', 1),
        (ids[0], 'T-pop Photocard 04', 'T-pop', 40, 1, 'Like New', '/assets/initial-stock-04.webp', 1),
        (ids[0], 'T-pop Photocard 05', 'T-pop', 40, 1, 'Like New', '/assets/initial-stock-05.webp', 1),
        (ids[1], 'K-pop Photocard 01', 'K-pop', 90, 1, 'Like New', '/assets/initial-stock-06.webp', 1),
        (ids[1], 'K-pop Photocard 02', 'K-pop', 90, 1, 'Like New', '/assets/initial-stock-07.webp', 1),
        (ids[1], 'K-pop Photocard 03', 'K-pop', 130, 2, 'Like New', '/assets/initial-stock-08.webp', 1),
        (ids[1], 'K-pop Photocard 04', 'K-pop', 130, 2, 'Like New', '/assets/initial-stock-09.webp', 1),
        (ids[1], 'K-pop Photocard 05', 'K-pop', 130, 1, 'Like New', '/assets/initial-stock-10.webp', 1),
    ]
    for store_id, title, category, price, stock, condition, image_url, verified in products:
        cur.execute(
            '''INSERT INTO products(store_id,title,category,price,stock,condition,image_url,verified,description,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (
                store_id,
                title,
                category,
                price,
                stock,
                condition,
                image_url,
                verified,
                'Initial Collectra stock. Product identity can be renamed later from seller/admin tools.',
                now_iso(),
            ),
        )

    cur.execute(
        'INSERT INTO reviews(store_id,device_id,rating,text,created_at) VALUES(?,?,?,?,?)',
        (ids[0], 'seed', 5, 'Clear listing photos and easy checkout flow.', now_iso()),
    )
    cur.execute(
        'INSERT INTO reviews(store_id,device_id,rating,text,created_at) VALUES(?,?,?,?,?)',
        (ids[1], 'seed2', 5, 'Verified shop profile and clear stock counts.', now_iso()),
    )

    # A dedicated demo store + pre-made order so public seller-workflow testers can jump
    # straight into recording a packing video without first creating products/orders themselves.
    demo_token = secrets.token_urlsafe(16)
    cur.execute(
        'INSERT INTO stores(name,description,logo_url,verified,category,token,created_at) VALUES(?,?,?,?,?,?,?)',
        ('Collectra Seller Demo Store', 'Sandbox store used for public seller-workflow testing. Not real inventory.',
         '/assets/collectra-logo.png', 1, 'K-pop', demo_token, now_iso()),
    )
    demo_store_id = cur.lastrowid
    cur.execute(
        '''INSERT INTO products(store_id,title,category,price,stock,condition,image_url,verified,description,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (demo_store_id, 'Demo Photocard (for testing)', 'K-pop', 50, 5, 'Like New', '/assets/initial-stock-06.webp', 1,
         'Sample item used only for the public seller-workflow demo.', now_iso()),
    )
    demo_product_id = cur.lastrowid
    cur.execute(
        '''INSERT INTO addresses(device_id,label,name,phone,line1,district,city,province,postal,is_default,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        ('demo-seller-buyer', 'Demo Address', 'Demo Tester', '0800000000', '123 Demo Rd', '', 'Bangkok', 'Bangkok', '10110', 1, now_iso()),
    )
    demo_address_id = cur.lastrowid
    cur.execute(
        'INSERT INTO orders(device_id,total,address_id,status,payment_status,created_at) VALUES(?,?,?,?,?,?)',
        ('demo-seller-buyer', 50, demo_address_id, 'Awaiting Packing', 'slip_uploaded', now_iso()),
    )
    demo_order_id = cur.lastrowid
    cur.execute(
        'INSERT INTO order_items(order_id,product_id,title,qty,unit_price) VALUES(?,?,?,?,?)',
        (demo_order_id, demo_product_id, 'Demo Photocard (for testing)', 1, 50),
    )
