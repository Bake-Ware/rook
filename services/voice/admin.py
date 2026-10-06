"""Voice administration: hashed credentials, cookie sessions, and live key registry."""
import asyncio
from collections import deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


class AdminStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY, salt TEXT, hash TEXT);
              CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, csrf TEXT, expires REAL);
              CREATE TABLE IF NOT EXISTS keys(id TEXT PRIMARY KEY, label TEXT, principal TEXT,
                worker TEXT, owner INTEGER, enabled INTEGER, created REAL);
            ''')

    def db(self):
        return sqlite3.connect(self.path, timeout=5)

    def bootstrap(self, name, password):
        salt = secrets.token_hex(16)
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO users VALUES(?,?,?)',
                       (name, salt, password_hash(password, salt)))

    def mappings(self, legacy):
        rows = dict(legacy)
        with self.db() as db:
            for key, label, principal, worker, owner, enabled, created in db.execute('SELECT * FROM keys'):
                if enabled:
                    rows[key] = {'principal': principal, 'worker': worker or None, 'owner': bool(owner)}
                else:
                    rows.pop(key, None)
        return rows

    def import_legacy(self, legacy):
        with self.db() as db:
            for key, row in legacy.items():
                db.execute('INSERT OR IGNORE INTO keys VALUES(?,?,?,?,?,?,?)',
                           (key, row.get('principal', 'Existing key'), row.get('principal', 'unmapped'),
                            row.get('worker'), row.get('owner') is True, 1, time.time()))


class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=1024)


class KeySettings(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    principal: str = Field(min_length=1, max_length=100)
    worker: str | None = Field(default=None, max_length=100)
    owner: bool = False


class PasswordChange(BaseModel):
    current: str = Field(max_length=1024)
    password: str = Field(min_length=1, max_length=1024)


def router(store, legacy, invalidate):
    routes = APIRouter()
    attempts = {}
    cookie = 'voice_admin'

    def mutation(request):
        # A custom header + JSON cannot be submitted cross-origin without CORS approval.
        if request.headers.get('x-voice-admin') != '1':
            raise HTTPException(403, 'Admin request header required')
        origin = request.headers.get('origin')
        if origin and origin != 'https://' + request.headers.get('host', ''):
            raise HTTPException(403, 'Cross-origin request denied')

    def session(request, write=False):
        token = request.cookies.get(cookie, '')
        with store.db() as db:
            row = db.execute('SELECT csrf,expires FROM sessions WHERE id=?', (digest(token),)).fetchone()
        if not row or row[1] <= time.time():
            raise HTTPException(401, 'Sign in to administer voice')
        if write:
            mutation(request)
            if not hmac.compare_digest(row[0], request.headers.get('x-csrf-token', '')):
                raise HTTPException(403, 'Session check failed')
        return row[0]

    @routes.get('/admin')
    async def page():
        return FileResponse(Path(__file__).with_name('admin.html'), headers={'Cache-Control': 'no-store'})

    @routes.post('/admin/api/login')
    async def login(data: Login, request: Request, response: Response):
        mutation(request)
        now = time.time()
        for address in list(attempts):
            while attempts[address] and attempts[address][0] < now - 600:
                attempts[address].popleft()
            if not attempts[address]:
                del attempts[address]
        ip = request.client.host if request.client else 'unknown'
        tries = attempts.setdefault(ip, deque())
        if len(tries) >= 10 or len(attempts) > 4096:
            raise HTTPException(429, 'Too many login attempts. Try again in ten minutes.')
        tries.append(now)
        with store.db() as db:
            row = db.execute('SELECT salt,hash FROM users WHERE name=?', (data.username,)).fetchone()
        candidate = await asyncio.to_thread(password_hash, data.password, row[0] if row else '00' * 16)
        if not row or not hmac.compare_digest(candidate, row[1]):
            raise HTTPException(401, 'Username or password incorrect')
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with store.db() as db:
            db.execute('DELETE FROM sessions WHERE expires<?', (now,))
            db.execute('INSERT INTO sessions VALUES(?,?,?)', (digest(token), csrf, now + 28800))
        response.set_cookie(cookie, token, secure=True, httponly=True, samesite='strict',
                            max_age=28800, path='/admin')
        return {'csrf': csrf}

    @routes.get('/admin/api/session')
    async def status(request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return {'csrf': session(request), 'open': os.environ.get('VOICE_ALLOW_ANONYMOUS') == '1'}

    @routes.post('/admin/api/logout')
    async def logout(request: Request, response: Response):
        session(request, True)
        with store.db() as db:
            db.execute('DELETE FROM sessions WHERE id=?', (digest(request.cookies[cookie]),))
        response.delete_cookie(cookie, path='/admin', secure=True, httponly=True, samesite='strict')
        return {'ok': True}

    @routes.get('/admin/api/keys')
    async def keys(request: Request, response: Response):
        session(request)
        response.headers['Cache-Control'] = 'no-store'
        with store.db() as db:
            rows = db.execute('SELECT * FROM keys ORDER BY created').fetchall()
        return {'keys': [dict(zip(('id', 'label', 'principal', 'worker', 'owner', 'enabled', 'created'), row)) for row in rows]}

    @routes.post('/admin/api/keys')
    async def create(data: KeySettings, request: Request, response: Response):
        session(request, True)
        token = secrets.token_urlsafe(32)
        with store.db() as db:
            db.execute('INSERT INTO keys VALUES(?,?,?,?,?,?,?)',
                       (digest(token), data.label, data.principal, data.worker or None, data.owner, 1, time.time()))
        response.headers['Cache-Control'] = 'no-store'
        return {'token': token, 'id': digest(token)}

    @routes.put('/admin/api/keys/{key}')
    async def update(key: str, data: KeySettings, request: Request):
        session(request, True)
        with store.db() as db:
            result = db.execute('UPDATE keys SET label=?,principal=?,worker=?,owner=? WHERE id=? AND enabled=1',
                                (data.label, data.principal, data.worker or None, data.owner, key))
            if not result.rowcount:
                raise HTTPException(404, 'Active key not found')
        await invalidate(key)
        return {'ok': True}

    @routes.delete('/admin/api/keys/{key}')
    async def revoke(key: str, request: Request):
        session(request, True)
        with store.db() as db:
            result = db.execute('UPDATE keys SET enabled=0 WHERE id=?', (key,))
            if not result.rowcount:
                raise HTTPException(404, 'Key not found')
        await invalidate(key)
        return {'ok': True}

    @routes.post('/admin/api/password')
    async def password(data: PasswordChange, request: Request, response: Response):
        session(request, True)
        with store.db() as db:
            name, salt, old = db.execute('SELECT * FROM users').fetchone()
        candidate = await asyncio.to_thread(password_hash, data.current, salt)
        if not hmac.compare_digest(candidate, old):
            raise HTTPException(403, 'Current password incorrect')
        salt = secrets.token_hex(16)
        hashed = await asyncio.to_thread(password_hash, data.password, salt)
        with store.db() as db:
            db.execute('UPDATE users SET salt=?,hash=? WHERE name=?', (salt, hashed, name))
            db.execute('DELETE FROM sessions')
        response.delete_cookie(cookie, path='/admin', secure=True, httponly=True, samesite='strict')
        return {'ok': True}

    return routes


if __name__ == '__main__':
    import argparse
    import getpass
    parser = argparse.ArgumentParser(description='Initialize a voice admin account (password prompted).')
    parser.add_argument('--db', default=os.environ.get('VOICE_ADMIN_DB'))
    parser.add_argument('--username', required=True)
    args = parser.parse_args()
    if not args.db:
        parser.error('--db or VOICE_ADMIN_DB is required')
    password = getpass.getpass('Admin password: ')
    if not password or password != getpass.getpass('Confirm password: '):
        parser.error('Passwords must be non-empty and match')
    AdminStore(args.db).bootstrap(args.username, password)
    print('Admin account initialized; existing accounts are preserved.')
