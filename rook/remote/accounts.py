"""Account identities, scoped band membership and expiring sessions.

Shares enrollment.db so credential changes and ownership checks use one SQLite
transaction. External identities are keyed by issuer/subject, never by email.
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager

from .enrollment import EnrollmentStore

GOOGLE = 'https://accounts.google.com'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError('Use a password between 12 and 1024 characters.')
    salt = salt or secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768,
                         r=8, p=1, maxmem=128*1024*1024).hex()
    return salt + ':' + key


def password_ok(password, encoded):
    try:
        salt, key = encoded.split(':')
        # Legacy bootstrap passwords may predate the new length requirement.
        if not isinstance(password, str) or len(password) > 1024:
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                                n=32768, r=8, p=1, maxmem=128*1024*1024).hex()
        return hmac.compare_digest(actual, key)
    except (ValueError, TypeError, AttributeError):
        return False


class AccountStore:
    def __init__(self, enrollment=None):
        self.enrollment = enrollment or EnrollmentStore()
        self.path = self.enrollment.path
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE, password TEXT,
                    name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
                    email_verified INTEGER NOT NULL DEFAULT 0,
                    admin INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                    avatar_source TEXT NOT NULL DEFAULT 'google',
                    avatar BLOB, avatar_updated REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS identities (
                    issuer TEXT NOT NULL, subject TEXT NOT NULL,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    picture TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(issuer,subject));
                CREATE TABLE IF NOT EXISTS memberships (
                    band_id TEXT NOT NULL REFERENCES bands(id),
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('owner','member')),
                    PRIMARY KEY(band_id,user_id));
                CREATE TABLE IF NOT EXISTS user_sessions (
                    hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf TEXT NOT NULL, expires REAL NOT NULL, authenticated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS account_grants (
                    hash TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS account_limits (
                    bucket TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS account_audit (
                    id INTEGER PRIMARY KEY, ts REAL NOT NULL, actor TEXT, action TEXT NOT NULL,
                    target TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS account_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS device_logins (
                    hash TEXT PRIMARY KEY, user_code TEXT UNIQUE NOT NULL,
                    expires REAL NOT NULL, user_id TEXT, last_poll REAL NOT NULL DEFAULT 0);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def audit(db, actor, action, target=''):
        db.execute('INSERT INTO account_audit(ts,actor,action,target) VALUES(?,?,?,?)',
                   (time.time(), actor, action, target))

    def rate_limit(self, peer, category='login', limit=10, global_limit=200):
        window = int(time.time() // 60)
        permitted = True
        with self.db() as db:
            db.execute('DELETE FROM account_limits WHERE window < ?', (window-1,))
            for bucket, cap in [(category+':global', global_limit), (category+':'+digest(peer), limit)]:
                row = db.execute('SELECT * FROM account_limits WHERE bucket=?', (bucket,)).fetchone()
                count = row['count'] if row and row['window'] == window else 0
                db.execute('INSERT OR REPLACE INTO account_limits VALUES(?,?,?)', (bucket,window,count+1))
                if count >= cap:
                    permitted = False
                    break
        return permitted

    def bootstrap(self, username, password):
        """Import operator once, only from trusted server configuration."""
        if not password:
            return None
        username = (username or 'operator').casefold()
        with self.db() as db:
            row = db.execute("SELECT value FROM account_settings WHERE key='bootstrap_user'").fetchone()
            if row:
                uid=row['value']
                current=db.execute('SELECT username,password FROM users WHERE id=?',(uid,)).fetchone()
                if not current:
                    raise ValueError('Configured operator account is missing.')
                if current['username']!=username or not password_ok(password,current['password']):
                    salt=secrets.token_hex(16)
                    key=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=32768,r=8,p=1,maxmem=128*1024*1024).hex()
                    db.execute('UPDATE users SET username=?,password=? WHERE id=?',(username,salt+':'+key,uid))
                    db.execute('DELETE FROM user_sessions WHERE user_id=?',(uid,))
                    self.audit(db,uid,'operator_credentials_updated')
                return uid
            uid = secrets.token_hex(16)
            salt = secrets.token_hex(16)
            key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768,
                                 r=8,p=1,maxmem=128*1024*1024).hex()
            db.execute('INSERT INTO users(id,username,password,name,admin,created) VALUES(?,?,?,?,1,?)',
                       (uid,username,salt+':'+key,username,time.time()))
            db.execute("INSERT INTO account_settings VALUES('bootstrap_user',?)", (uid,))
            db.execute("INSERT INTO memberships SELECT id,?,'owner' FROM bands", (uid,))
            self.audit(db,uid,'bootstrap')
            return uid

    def create_local(self, username, password, name=''):
        username = str(username).strip().casefold()
        if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,63}', username):
            raise ValueError('Username must be 3–64 letters, digits, dots, dashes or underscores.')
        encoded = password_hash(password)
        uid = secrets.token_hex(16)
        try:
            with self.db() as db:
                db.execute('INSERT INTO users(id,username,password,name,created) VALUES(?,?,?,?,?)',
                           (uid,username,encoded,str(name or username)[:100],time.time()))
                self.audit(db,uid,'register')
        except sqlite3.IntegrityError:
            raise ValueError('Username is unavailable.') from None
        return uid

    def login(self, username, password):
        with self.db() as db:
            row = db.execute('SELECT id,password FROM users WHERE username=?',
                             (str(username).strip().casefold(),)).fetchone()
        # Equalize the expensive work for unknown usernames.
        encoded = row['password'] if row and row['password'] else '00'*16+':'+'00'*64
        return row['id'] if password_ok(password,encoded) and row else None

    def user(self, uid):
        with self.db() as db:
            row = db.execute('SELECT id,username,name,email,email_verified,admin,created,avatar_source,avatar_updated,password IS NOT NULL AS has_password FROM users WHERE id=?', (uid,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['google_connected'] = bool(db.execute('SELECT 1 FROM identities WHERE user_id=? AND issuer=?', (uid,GOOGLE)).fetchone())
            return result

    def session(self, token):
        if not token:
            return None
        with self.db() as db:
            row = db.execute('SELECT * FROM user_sessions WHERE hash=? AND expires>?',
                             (digest(token),time.time())).fetchone()
        if not row:
            return None
        user = self.user(row['user_id'])
        if user:
            user.update(csrf=row['csrf'],authenticated=row['authenticated'])
        return user

    def new_session(self, uid):
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('DELETE FROM user_sessions WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO user_sessions VALUES(?,?,?,?,?)',
                       (digest(token),uid,secrets.token_urlsafe(32),time.time()+30*86400,time.time()))
        return token

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM user_sessions WHERE hash=?', (digest(token),))

    def grant(self, kind, payload, ttl=600):
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('DELETE FROM account_grants WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO account_grants VALUES(?,?,?,?)',
                       (digest(token),kind,json.dumps(payload),time.time()+ttl))
        return token

    def consume(self, token, kind):
        with self.db() as db:
            row = db.execute('SELECT payload FROM account_grants WHERE hash=? AND kind=? AND expires>?',
                             (digest(token),kind,time.time())).fetchone()
            if not row:
                raise ValueError('This request expired or was already used.')
            db.execute('DELETE FROM account_grants WHERE hash=?', (digest(token),))
            return json.loads(row['payload'])

    def google_identity(self, claims, link_user=None):
        subject = claims['sub']
        with self.db() as db:
            existing = db.execute('SELECT user_id FROM identities WHERE issuer=? AND subject=?', (GOOGLE,subject)).fetchone()
            if existing and link_user and existing['user_id'] != link_user:
                return {'merge_from':existing['user_id'], 'merge_to':link_user}
            uid = existing['user_id'] if existing else link_user or secrets.token_hex(16)
            if not existing and not link_user:
                db.execute('INSERT INTO users(id,name,email,email_verified,created) VALUES(?,?,?,?,?)',
                           (uid,str(claims.get('name') or 'Rook user')[:100],claims.get('email',''),int(claims.get('email_verified') is True),time.time()))
            db.execute('INSERT INTO identities VALUES(?,?,?,?) ON CONFLICT(issuer,subject) DO UPDATE SET picture=excluded.picture',
                       (GOOGLE,subject,uid,claims.get('picture','')))
            self.audit(db,uid,'google_link' if link_user else 'google_login')
            return {'user_id':uid}

    def merge(self, source, target):
        """Caller must supply a consumed merge grant proving both logins."""
        with self.db() as db:
            src = db.execute('SELECT * FROM users WHERE id=?', (source,)).fetchone()
            dst = db.execute('SELECT * FROM users WHERE id=?', (target,)).fetchone()
            if not src or not dst or source == target:
                raise ValueError('Accounts unavailable for merge.')
            # Keep the target's local credentials; never silently discard another local login.
            if src['username'] and dst['username']:
                raise ValueError('Both accounts have local usernames. Contact the operator to reconcile them.')
            for m in db.execute('SELECT * FROM memberships WHERE user_id=?',(source,)).fetchall():
                db.execute("INSERT INTO memberships VALUES(?,?,?) ON CONFLICT(band_id,user_id) DO UPDATE SET role=CASE WHEN memberships.role='owner' OR excluded.role='owner' THEN 'owner' ELSE 'member' END", (m['band_id'],target,m['role']))
            db.execute('UPDATE identities SET user_id=? WHERE user_id=?',(target,source))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='devices'").fetchone():
                db.execute('UPDATE devices SET sponsor=? WHERE sponsor=?',(target,source))
            db.execute('DELETE FROM device_logins WHERE user_id IN (?,?)',(source,target))
            if src['username']:
                db.execute('UPDATE users SET username=NULL WHERE id=?',(source,))
                db.execute('UPDATE users SET username=?,password=? WHERE id=?',(src['username'],src['password'],target))
            if src['admin']:
                db.execute('UPDATE users SET admin=1 WHERE id=?',(target,))
            if dst['avatar_source']=='google' and src['avatar_source']=='custom':
                db.execute("UPDATE users SET avatar_source='custom',avatar=?,avatar_updated=? WHERE id=?",(src['avatar'],src['avatar_updated'],target))
            db.execute("UPDATE account_settings SET value=? WHERE key='bootstrap_user' AND value=?",(target,source))
            db.execute('DELETE FROM user_sessions WHERE user_id IN (?,?)',(source,target))
            db.execute('DELETE FROM users WHERE id=?',(source,))
            self.audit(db,target,'account_merge',source)

    def require_band(self, uid, band_id, owner=False, db=None):
        if db is None:
            with self.db() as connection:
                return self.require_band(uid,band_id,owner,connection)
        row = db.execute('SELECT m.role,b.active FROM memberships m JOIN bands b ON b.id=m.band_id WHERE m.user_id=? AND m.band_id=? AND b.deleted=0', (uid,band_id)).fetchone()
        if not row or (owner and row['role']!='owner'):
            raise PermissionError('Band access denied.')
        return row['role']

    def bands(self, uid, configs=False):
        with self.db() as db:
            fields = ',b.psk' if configs else ''
            rows = db.execute('SELECT b.id,b.name,b.hub,b.epoch,b.active,b.psk_hash,m.role'+fields+' FROM bands b JOIN memberships m ON m.band_id=b.id WHERE m.user_id=? AND b.deleted=0', (uid,)).fetchall()
        return [{**dict(r),'label':r['psk_hash'][:8]} for r in rows if r['active'] or not configs]

    @staticmethod
    def band_name(name):
        if not isinstance(name,str) or not name.strip() or len(name.strip())>100:
            raise ValueError('Use a band name between 1 and 100 characters.')
        return name.strip()

    def create_band(self,uid,name,hub):
        from .psk import generate_psk
        name=self.band_name(name)
        bid=secrets.token_hex(16);psk=generate_psk()
        with self.db() as db:
            db.execute('INSERT INTO bands(id,name,psk,psk_hash,hub) VALUES(?,?,?,?,?)',(bid,name,psk,digest(psk),hub))
            db.execute("INSERT INTO memberships VALUES(?,?,'owner')",(bid,uid))
            self.audit(db,uid,'band_create',bid)
        return bid

    def rename_band(self,uid,bid,name):
        name=self.band_name(name)
        with self.db() as db:
            self.require_band(uid,bid,True,db)
            if not db.execute('SELECT 1 FROM bands WHERE id=? AND deleted=0',(bid,)).fetchone():
                raise ValueError('Band was deleted.')
            db.execute('UPDATE bands SET name=? WHERE id=?',(name,bid))
            self.audit(db,uid,'band_rename',bid)

    def delete_band(self,uid,bid):
        with self.db() as db:
            self.require_band(uid,bid,True,db)
            band=db.execute('SELECT * FROM bands WHERE id=?',(bid,)).fetchone()
            if band['is_primary']:raise ValueError('The configured primary band cannot be deleted.')
            if band['deleted']:return
            if db.execute("SELECT 1 FROM band_migrations WHERE (band_id=? OR target_band_id=?) AND phase IN ('prepared','active')",(bid,bid)).fetchone():
                raise ValueError('Finish or cancel this band’s migration before deleting it.')
            db.execute('INSERT OR IGNORE INTO retired VALUES(?)',(band['psk_hash'],))
            db.execute("UPDATE bands SET active=0,deleted=1,psk='',epoch=epoch+1 WHERE id=?",(bid,))
            db.execute('DELETE FROM pairing WHERE band_id=?',(bid,))
            db.execute('UPDATE devices SET active=0 WHERE band_id=?',(bid,))
            self.audit(db,uid,'band_delete',bid)

    def assign(self, band_id, uid):
        with self.db() as db:
            db.execute("INSERT INTO memberships VALUES(?,?,'owner')",(band_id,uid))
            self.audit(db,uid,'band_create',band_id)

    def members(self, uid, band_id):
        with self.db() as db:
            self.require_band(uid,band_id,True,db)
            return [dict(r) for r in db.execute('SELECT u.id,u.name,u.username,m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.band_id=?',(band_id,))]

    def invite(self, uid, band_id):
        self.require_band(uid,band_id,True)
        return self.grant('invite', {'band_id':band_id,'inviter':uid},86400*7)

    def accept_invite(self, uid, token):
        # Atomic consume and membership insertion; recheck inviter's current authority.
        with self.db() as db:
            row=db.execute("SELECT payload FROM account_grants WHERE hash=? AND kind='invite' AND expires>?",(digest(token),time.time())).fetchone()
            if not row:
                raise ValueError('Invitation expired or already used.')
            payload=json.loads(row['payload'])
            self.require_band(payload['inviter'],payload['band_id'],True,db)
            db.execute("INSERT OR IGNORE INTO memberships VALUES(?,?,'member')",(payload['band_id'],uid))
            db.execute('DELETE FROM account_grants WHERE hash=?',(digest(token),))
            self.audit(db,uid,'invite_accept',payload['band_id'])

    def change_member(self, actor, band_id, uid, role):
        if role not in ('owner','member','remove'):
            raise ValueError('Invalid role.')
        with self.db() as db:
            self.require_band(actor,band_id,True,db)
            old = db.execute('SELECT role FROM memberships WHERE band_id=? AND user_id=?',(band_id,uid)).fetchone()
            if not old:
                raise ValueError('Member not found.')
            if old['role']=='owner' and role!='owner':
                count=db.execute("SELECT count(*) FROM memberships WHERE band_id=? AND role='owner'",(band_id,)).fetchone()[0]
                if count<=1:
                    raise ValueError('A band must retain an owner.')
            if role=='remove':
                db.execute('DELETE FROM memberships WHERE band_id=? AND user_id=?',(band_id,uid))
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='devices'").fetchone():
                    db.execute('UPDATE devices SET active=0 WHERE band_id=? AND sponsor=?',(band_id,uid))
            else:
                db.execute('UPDATE memberships SET role=? WHERE band_id=? AND user_id=?',(role,band_id,uid))
            self.audit(db,actor,'member_'+role,uid)

    def set_password(self, uid, password):
        encoded=password_hash(password)
        with self.db() as db:
            row=db.execute('SELECT username FROM users WHERE id=?',(uid,)).fetchone()
            if not row or not row['username']:
                raise ValueError('Set a local username first.')
            db.execute('UPDATE users SET password=? WHERE id=?',(encoded,uid))
            db.execute('DELETE FROM user_sessions WHERE user_id=?',(uid,))

    def add_local(self, uid, username, password):
        username=str(username).strip().casefold()
        if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,63}',username):
            raise ValueError('Invalid username.')
        encoded=password_hash(password)
        try:
            with self.db() as db:
                row=db.execute('SELECT username FROM users WHERE id=?',(uid,)).fetchone()
                if not row or row['username']:
                    raise ValueError('A local login is already connected.')
                db.execute('UPDATE users SET username=?,password=? WHERE id=?',(username,encoded,uid))
        except sqlite3.IntegrityError:
            raise ValueError('Username is unavailable.') from None

    def unlink_google(self, uid):
        with self.db() as db:
            row=db.execute('SELECT password FROM users WHERE id=?',(uid,)).fetchone()
            if not row or not row['password']:
                raise ValueError('Connect a local login before disconnecting Google.')
            db.execute('DELETE FROM identities WHERE user_id=? AND issuer=?',(uid,GOOGLE))
            db.execute("UPDATE users SET avatar=NULL,avatar_source='initials',avatar_updated=? WHERE id=? AND avatar_source='google'",(time.time(),uid))
            self.audit(db,uid,'google_unlink')

    def avatar(self, uid):
        with self.db() as db:
            row=db.execute('SELECT avatar,avatar_updated FROM users WHERE id=?',(uid,)).fetchone()
            return (row['avatar'],row['avatar_updated']) if row and row['avatar'] else None

    def set_avatar(self, uid, source, data=None):
        if source not in ('google','custom','initials'):
            raise ValueError('Invalid avatar source.')
        with self.db() as db:
            db.execute('UPDATE users SET avatar_source=?,avatar=?,avatar_updated=? WHERE id=?',(source,data,time.time(),uid))

    def picture(self, uid):
        with self.db() as db:
            row=db.execute('SELECT picture FROM identities WHERE user_id=? AND issuer=?',(uid,GOOGLE)).fetchone()
            return row['picture'] if row else ''

    def device_login_start(self):
        token=secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('DELETE FROM device_logins WHERE expires<?',(time.time(),))
            while True:
                code=''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))
                if not db.execute('SELECT 1 FROM device_logins WHERE user_code=?',(code,)).fetchone():break
            db.execute('INSERT INTO device_logins(hash,user_code,expires) VALUES(?,?,?)',(digest(token),code,time.time()+600))
        return {'device_code':token,'user_code':code,'expires_in':600,'interval':5}

    def device_login_approve(self,uid,code):
        with self.db() as db:
            row=db.execute('SELECT hash FROM device_logins WHERE user_code=? AND expires>? AND user_id IS NULL',(code.upper(),time.time())).fetchone()
            if not row:raise ValueError('Device request expired or already approved.')
            db.execute('UPDATE device_logins SET user_id=? WHERE hash=?',(uid,row['hash']))
            self.audit(db,uid,'device_login_approve')

    def device_login_poll(self,token):
        with self.db() as db:
            row=db.execute('SELECT * FROM device_logins WHERE hash=? AND expires>?',(digest(token),time.time())).fetchone()
            if not row:return {'error':'expired_token'}
            if time.time()-row['last_poll']<5:return {'error':'slow_down'}
            db.execute('UPDATE device_logins SET last_poll=? WHERE hash=?',(time.time(),row['hash']))
            if not row['user_id']:return {'error':'authorization_pending'}
            uid=row['user_id']
            db.execute('DELETE FROM device_logins WHERE hash=?',(row['hash'],))
        if not self.user(uid):return {'error':'expired_token'}
        bands=self.bands(uid,configs=True)
        return {'bands':bands,'enrollment_grants':{band['id']:self.grant('device_enroll',{'user_id':uid,'band_id':band['id']},300) for band in bands}}
