"""User-facing accounts and band configuration portal for the aiohttp server."""
import asyncio
import base64
import hmac
import html
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode

from aiohttp import web

from .accounts import AccountStore, digest
from .google_auth import GoogleAuth, fetch_avatar, normalize_avatar
from .psk import generate_psk
from .enrollment import JoinDenied, JoinLimited

log=logging.getLogger(__name__)
COOKIE='rook_account'
NO_STORE={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff'}


def esc(value):
    return html.escape(str(value),quote=True)


def document(title, body):
    return '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(title)+' · Rook</title><style>body{font:16px system-ui;background:#0b0f14;color:#e6edf3;margin:0}main{max-width:850px;margin:auto;padding:24px}a{color:#48d8b5}h1,h2{font-weight:600}h2{margin-top:32px}section{padding:20px;border:1px solid #29333e;border-radius:12px;margin:16px 0;background:#111820}input,select,button{font:inherit;padding:10px;border-radius:6px;border:1px solid #384553;background:#192431;color:#e6edf3;max-width:100%;box-sizing:border-box}input{margin:5px 0}button{cursor:pointer;background:#12634f}label{display:block;margin-top:10px}form{margin:14px 0}code,pre{white-space:pre-wrap;overflow-wrap:anywhere}small,.muted{color:#a8b4bf}.avatar{width:64px;height:64px;object-fit:cover;border-radius:50%}.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.error{color:#ffa1a1}nav{display:flex;gap:18px;flex-wrap:wrap}hr{border:0;border-top:1px solid #29333e}</style></head><body><main><nav><a href="/account">♖ Rook account</a><a href="/account#bands">Bands & pairing</a><a href="/">Dashboard</a></nav><h1>'+esc(title)+'</h1>'+body+'</main></body></html>'


class AccountWeb:
    def __init__(self, server):
        self.server=server
        self.store=AccountStore(server._enrollment)
        self.bootstrap_id=self.store.bootstrap(server.web_user,server.web_pass)
        self.google=GoogleAuth()
        from .devices import DeviceStore
        self.devices=DeviceStore(self.store)
        self.origin='https://'+server.domain
        self.callback=self.origin+'/auth/google/callback'
        self.google_web_login=os.environ.get('ROOK_GOOGLE_WEB_LOGIN','1')=='1'

    def handles(self,path):
        return path=='/account' or path.startswith('/account/') or path.startswith('/auth/')

    def install(self,app):
        app.router.add_route('*','/account',self.page)
        app.router.add_get('/account/login',self.login_page)
        app.router.add_post('/account/login',self.local_login)
        app.router.add_post('/account/register',self.register)
        app.router.add_post('/account/action',self.action)
        app.router.add_get('/account/avatar/{uid}',self.avatar)
        app.router.add_get('/account/configurations',self.configurations)
        app.router.add_get('/auth/google',self.google_start)
        app.router.add_get('/auth/google/callback',self.google_callback)
        app.router.add_post('/auth/google/native',self.google_native)
        app.router.add_post('/auth/google/challenge',self.google_challenge)
        app.router.add_get('/account/session',self.session_info)
        app.router.add_post('/account/pairing',self.pairing_refresh)
        app.router.add_post('/auth/device/start',self.device_start)
        app.router.add_post('/auth/device/poll',self.device_poll)
        app.router.add_post('/auth/devices/enroll',self.device_enroll)
        app.router.add_post('/auth/devices/challenge',self.device_challenge)
        app.router.add_post('/auth/devices/config',self.device_config)
        app.router.add_post('/auth/devices/renew',self.device_renew)

    def current(self,request):
        token=request.cookies.get(COOKIE,'')
        if request.headers.get('Authorization','').startswith('Bearer '):
            token=request.headers['Authorization'][7:]
        return self.store.session(token)

    def require(self,request,fresh=False):
        user=self.current(request)
        if not user:
            raise web.HTTPFound('/account/login',headers=NO_STORE)
        if fresh and time.time()-user['authenticated']>600:
            raise ValueError('Sign in again before changing connected logins.')
        return user

    def response(self,title,body,status=200):
        # Same-origin form POSTs need their Origin preserved for CSRF checks.
        return web.Response(text=document(title,body),content_type='text/html',status=status,
                            headers={**NO_STORE,'Referrer-Policy':'origin'})

    def redirect(self,path='/account'):
        return web.HTTPFound(path,headers=NO_STORE)

    def signed_in(self,uid):
        response=self.redirect()
        response.set_cookie(COOKIE,self.store.new_session(uid),max_age=30*86400,secure=True,httponly=True,samesite='Lax')
        return response

    def csrf(self,request,data,user):
        if request.headers.get('Origin',self.origin)!=self.origin:
            raise PermissionError('Request origin did not match.')
        if not hmac.compare_digest(str(data.get('csrf','')),user['csrf']):
            raise PermissionError('Form expired; reload the page.')

    def form(self,user,op,body,band_id=''):
        return '<form method="post" action="/account/action"><input type="hidden" name="csrf" value="'+esc(user['csrf'])+'"><input type="hidden" name="op" value="'+esc(op)+'"><input type="hidden" name="band_id" value="'+esc(band_id)+'">'+body+'</form>'

    async def login_page(self,request):
        token=secrets.token_urlsafe(32)
        body='<p>Sign in to fetch your band configurations and manage your account.</p>'
        if self.google.enabled and self.google_web_login:
            body+='<p><a href="/auth/google">Sign in with Google</a></p>'
        for action,title in [('login','Sign in locally'),('register','Create local account')]:
            body+='<section><h2>'+title+'</h2><form method="post" action="/account/'+action+'"><input type="hidden" name="form_token" value="'+token+'"><label>Username<input name="username" autocomplete="username" required></label><label>Password<input name="password" type="password" autocomplete="'+('current-password' if action=='login' else 'new-password')+'" required></label><button>'+title+'</button></form></section>'
        response=self.response('Sign in',body)
        response.set_cookie('rook_login_form',token,secure=True,httponly=True,samesite='Lax',max_age=600)
        return response

    def login_form_ok(self,request,data):
        if request.headers.get('Origin',self.origin)!=self.origin:
            return False
        cookie=request.cookies.get('rook_login_form','')
        return bool(cookie) and hmac.compare_digest(cookie,str(data.get('form_token','')))

    async def local_login(self,request):
        data=await request.post()
        if not self.login_form_ok(request,data):
            return self.response('Sign in','<p>Reload the sign-in form and try again.</p>',403)
        if not self.store.rate_limit(request.remote or 'unknown'):
            return self.response('Try again shortly','<p>Too many attempts. Wait a minute.</p>',429)
        uid=await asyncio.to_thread(self.store.login,data.get('username',''),data.get('password',''))
        if not uid:
            return self.response('Sign in','<p>Invalid credentials. <a href="/account/login">Try again</a>.</p>',401)
        return self.signed_in(uid)

    async def register(self,request):
        data=await request.post()
        if not self.login_form_ok(request,data):
            return self.response('Sign in','<p>Reload the sign-in form and try again.</p>',403)
        if not self.store.rate_limit(request.remote or 'unknown','register',3):
            return self.response('Try again shortly','<p>Too many attempts.</p>',429)
        try:
            uid=await asyncio.to_thread(self.store.create_local,data.get('username',''),data.get('password',''))
            return self.signed_in(uid)
        except ValueError as error:
            return self.response('Create account','<p>'+esc(error)+'</p><a href="/account/login">Try again</a>',400)

    async def google_start(self,request):
        if not self.google.enabled or not self.google_web_login:
            raise web.HTTPNotFound()
        if not self.store.rate_limit(request.remote or 'unknown','oauth',20):
            raise web.HTTPTooManyRequests()
        link_user=None
        if request.query.get('link')=='1':
            try:
                link_user=self.require(request,fresh=True)['id']
            except ValueError as error:
                return self.response('Sign in again','<p>'+esc(error)+'</p><a href="/account/login">Sign in</a>',403)
        binding=secrets.token_urlsafe(32)
        nonce=secrets.token_urlsafe(32)
        verifier=secrets.token_urlsafe(48)
        state=self.store.grant('oauth',{'binding':digest(binding),'nonce':nonce,'verifier':verifier,'link_user':link_user})
        response=self.redirect(self.google.authorize_url(state,nonce,verifier,self.callback))
        response.set_cookie('rook_oauth',binding,max_age=600,secure=True,httponly=True,samesite='Lax')
        return response

    async def refresh_avatar(self,uid,force=False):
        user=self.store.user(uid)
        if user['avatar_source']!='google' or (not force and time.time()-user['avatar_updated']<86400):
            return
        picture=self.store.picture(uid)
        if not picture:
            return
        try:
            raw=await fetch_avatar(picture)
            # Do not overwrite a custom image set while the download was pending.
            with self.store.db() as db:
                db.execute("UPDATE users SET avatar=?,avatar_updated=? WHERE id=? AND avatar_source='google'",(raw,time.time(),uid))
        except (ValueError,OSError,TimeoutError) as error:
            log.info('Google avatar unavailable (%s)',type(error).__name__)
        except Exception as error:
            log.warning('Google avatar fetch failed (%s)',type(error).__name__)

    async def google_callback(self,request):
        try:
            data=self.store.consume(request.query.get('state',''),'oauth')
            if not hmac.compare_digest(data['binding'],digest(request.cookies.get('rook_oauth',''))):
                raise ValueError('Sign-in browser did not match. Please start again.')
            if data['link_user']:
                current=self.require(request,fresh=True)
                if current['id']!=data['link_user']:
                    raise ValueError('Account changed while connecting Google.')
            if request.query.get('error'):
                raise ValueError('Google sign-in was cancelled.')
            claims=await self.google.exchange(request.query.get('code',''),data['verifier'],self.callback,data['nonce'])
            result=self.store.google_identity(claims,data['link_user'])
            if 'merge_from' in result:
                grant=self.store.grant('merge',result)
                user=self.require(request,fresh=True)
                body='<p>This Google login already belongs to another Rook account. Merge its band memberships into this account?</p>'
                body+=self.form(user,'merge','<input type="hidden" name="grant" value="'+esc(grant)+'"><button>Merge accounts</button>')
                response=self.response('Connect accounts',body)
            else:
                await self.refresh_avatar(result['user_id'])
                response=self.signed_in(result['user_id'])
            response.del_cookie('rook_oauth')
            return response
        except (ValueError,PermissionError) as error:
            return self.response('Google sign-in','<p>'+esc(error)+'</p><a href="/account/login">Try again</a>',400)
        except Exception as error:
            log.warning('Google sign-in failed (%s)',type(error).__name__)
            return self.response('Google sign-in','<p>Google sign-in is temporarily unavailable. Try again or use your local login.</p>',503)

    async def google_challenge(self,request):
        if not self.google.enabled:
            raise web.HTTPNotFound()
        if not self.store.rate_limit(request.remote or 'unknown','native',10):
            raise web.HTTPTooManyRequests()
        nonce=secrets.token_urlsafe(32)
        challenge=self.store.grant('native_google',{'nonce':nonce})
        return web.json_response({'challenge':challenge,'nonce':nonce,'client_id':self.google.client_id},headers=NO_STORE)

    async def google_native(self,request):
        if not self.google.enabled:
            raise web.HTTPNotFound()
        if not self.store.rate_limit(request.remote or 'unknown','native',10):
            raise web.HTTPTooManyRequests()
        try:
            body=await request.json()
            challenge=self.store.consume(body.get('challenge',''),'native_google')
            claims=await self.google.verify(body.get('id_token',''),challenge['nonce'])
            uid=self.store.google_identity(claims)['user_id']
            await self.refresh_avatar(uid)
            return web.json_response({'token':self.store.new_session(uid),'user':self.store.user(uid),
                                     'bands':self.store.bands(uid,configs=True)},headers=NO_STORE)
        except (ValueError,TypeError,AttributeError):
            return web.json_response({'error':'Google sign-in failed.'},status=400,headers=NO_STORE)

    async def session_info(self,request):
        user=self.current(request)
        if not user:
            return web.json_response({'error':'Sign in required.'},status=401,headers=NO_STORE)
        return web.json_response({'user':user,'bands':self.store.bands(user['id'])},headers=NO_STORE)

    async def device_start(self,request):
        if not self.store.rate_limit(request.remote or 'unknown','device_start',5):
            raise web.HTTPTooManyRequests()
        grant=self.store.device_login_start()
        grant['verification_uri']=self.origin+'/account'
        grant['verification_uri_complete']=self.origin+'/account?device='+grant['user_code']
        return web.json_response(grant,headers=NO_STORE)

    async def device_poll(self,request):
        if not self.store.rate_limit(request.remote or 'unknown','device_poll',30):
            raise web.HTTPTooManyRequests()
        try:
            data=await request.json()
            result=self.store.device_login_poll(str(data.get('device_code','')))
            return web.json_response(result,headers=NO_STORE)
        except (ValueError,AttributeError):
            return web.json_response({'error':'invalid_request'},status=400,headers=NO_STORE)

    async def device_enroll(self,request):
        if not self.store.rate_limit(request.remote or 'unknown','certificate_enroll',10):
            raise web.HTTPTooManyRequests()
        try:
            data=await request.json()
            user=self.current(request)
            if data.get('enrollment_grant'):
                grant=self.store.consume(str(data['enrollment_grant']),'device_enroll')
                bid=grant['band_id'];sponsor=grant['user_id']
                self.store.require_band(sponsor,bid)
            elif user:
                if not request.headers.get('Authorization','').startswith('Bearer '):self.csrf(request,data,user)
                bid=str(data.get('band_id',''));sponsor=user['id']
                self.store.require_band(sponsor,bid)
            else:
                band=self.server._enrollment.redeem(str(data.get('code','')),request.remote or 'unknown')
                bid=band['id'];sponsor=None
            result=self.devices.enroll(bid,sponsor,data.get('csr',''),data.get('name','worker'))
            return web.json_response(result,headers=NO_STORE)
        except JoinLimited:
            return web.json_response({'error':'Too many enrollment attempts.'},status=429,headers=NO_STORE)
        except (PermissionError,JoinDenied):
            return web.json_response({'error':'Device enrollment denied.'},status=403,headers=NO_STORE)
        except (ValueError,TypeError,AttributeError):
            return web.json_response({'error':'Invalid or expired enrollment request.'},status=400,headers=NO_STORE)

    async def device_challenge(self,request):
        if not self.store.rate_limit(request.remote or 'unknown','device_proof',300,1000):
            raise web.HTTPTooManyRequests()
        try:
            data=await request.json()
            return web.json_response(self.devices.challenge(str(data.get('device_id','')),str(data.get('purpose',''))),headers=NO_STORE)
        except (ValueError,AttributeError):
            return web.json_response({'error':'Invalid device challenge.'},status=400,headers=NO_STORE)

    async def device_config(self,request):
        return await self.device_operation(request,'config')

    async def device_renew(self,request):
        return await self.device_operation(request,'renew')

    async def device_operation(self,request,operation):
        if not self.store.rate_limit(request.remote or 'unknown','device_operation',300,1000):
            raise web.HTTPTooManyRequests()
        try:
            data=await request.json()
            result=getattr(self.devices,operation)(data)
            return web.json_response(result,headers=NO_STORE)
        except (ValueError,TypeError,AttributeError,PermissionError):
            return web.json_response({'error':'Device proof expired or access revoked.'},status=403,headers=NO_STORE)

    async def pairing_refresh(self,request):
        user=self.require(request)
        try:
            data=await request.json()
            self.csrf(request,data,user)
            bid=str(data.get('band_id',''))
            self.store.require_band(user['id'],bid,True)
            grant=self.server._enrollment.issue(bid,session=str(data.get('session','')))
            return web.json_response(grant,headers=NO_STORE)
        except PermissionError as error:
            return web.json_response({'error':str(error)},status=403,headers=NO_STORE)
        except (ValueError,TypeError,AttributeError) as error:
            return web.json_response({'error':str(error)},status=400,headers=NO_STORE)

    async def configurations(self,request):
        user=self.require(request)
        bands=self.store.bands(user['id'],configs=True)
        selected=request.query.get('band')
        if selected:
            try:
                self.store.require_band(user['id'],selected)
            except PermissionError:
                raise web.HTTPForbidden(text='Band access denied.',headers=NO_STORE)
            bands=[b for b in bands if b['id']==selected]
        return web.json_response({'bands':bands},headers={**NO_STORE,'Content-Disposition':'attachment; filename="rook-configurations.json"'})

    async def avatar(self,request):
        user=self.require(request)
        target=request.match_info['uid']
        if target!=user['id']:
            with self.store.db() as db:
                if not db.execute('SELECT 1 FROM memberships a JOIN memberships b ON a.band_id=b.band_id WHERE a.user_id=? AND b.user_id=?',(user['id'],target)).fetchone():
                    raise web.HTTPNotFound()
        image=self.store.avatar(target)
        if not image:
            raise web.HTTPNotFound()
        return web.Response(body=image[0],content_type='image/jpeg',headers={'Cache-Control':'private, max-age=300','X-Content-Type-Options':'nosniff'})

    async def page(self,request):
        if request.method!='GET':
            raise web.HTTPMethodNotAllowed(request.method,['GET'])
        user=self.require(request)
        avatar=self.store.avatar(user['id'])
        body='<div class="row">'+('<img class="avatar" src="/account/avatar/'+esc(user['id'])+'?v='+str(user['avatar_updated'])+'" alt="Your avatar">' if avatar else '<span class="avatar">'+esc(user['name'][:2].upper())+'</span>')+'<p>'+esc(user['name'])+'</p></div>'
        body+=self.form(user,'logout','<button>Sign out</button>')
        device=request.query.get('device','')
        body+='<section><h2>Authorize a terminal installer</h2><p>Only approve a code shown by an installer you started. Approval lets that installer fetch all your authorized band configurations once.</p>'+self.form(user,'device_approve','<label>Installer authorization code <input name="user_code" value="'+esc(device)+'" maxlength="8" required></label><button>Authorize configuration fetch</button>')+'</section>'
        body+='<section><h2>Profile & connected logins</h2>'
        body+=self.form(user,'profile','<label>Display name <input name="name" value="'+esc(user['name'])+'" maxlength="100" required></label><button>Save name</button>')
        if self.google.enabled:
            body+=('<p>Google connected.</p>'+self.form(user,'unlink','<button>Disconnect Google</button>') if user['google_connected'] else '<p><a href="/auth/google?link=1">Connect Google account</a></p>')
        if not user['has_password']:
            body+=self.form(user,'local','<label>Local username <input name="username" autocomplete="username" required></label><label>Password <input name="password" type="password" minlength="12" autocomplete="new-password" required></label><button>Add local login</button>')
        else:
            body+=self.form(user,'password','<label>Current password <input type="password" name="current_password" autocomplete="current-password" required></label><label>New password <input type="password" name="password" minlength="12" autocomplete="new-password" required></label><button>Change password</button>')
        body+=self.form(user,'avatar','<label>Avatar <select name="source"><option value="google">Use Google photo</option><option value="initials">Use initials</option></select></label><button>Apply avatar</button>')
        body+='<form method="post" action="/account/action" enctype="multipart/form-data"><input type="hidden" name="csrf" value="'+esc(user['csrf'])+'"><input type="hidden" name="op" value="avatar_upload"><label>Custom avatar <input type="file" name="image" accept="image/png,image/jpeg,image/webp" required></label><button>Upload avatar</button></form></section>'
        invite=request.query.get('invite','')
        body+='<section><h2>Join an invited band</h2>'+self.form(user,'accept','<label>Invitation code <input name="invite" value="'+esc(invite)+'" required></label><button>Accept invitation</button>')+'</section>'
        body+='<h2 id="bands">Your bands</h2><p><a href="/account/configurations">Download all configurations</a></p>'
        for band in self.store.bands(user['id']):
            bid=band['id']
            body+='<section><h2>'+esc(band['name'])+'</h2><p>'+esc(band['role'])+' · epoch '+str(band['epoch'])+(' · revoked' if not band['active'] else '')+'</p>'
            if band['active']:
                body+='<p><a href="/account/configurations?band='+bid+'">Download configuration</a></p>'
            if band['role']=='owner':
                body+=self.form(user,'pair','<button>Show pairing code</button>',bid)
                body+=self.form(user,'pair_revoke','<button>Revoke pairing code</button>',bid)
                body+=self.form(user,'invite','<button>Create invitation link</button>',bid)
                body+='<details><summary>Members and ownership</summary>'
                for member in self.store.members(user['id'],bid):
                    body+=self.form(user,'member','<input type="hidden" name="user_id" value="'+member['id']+'"><span>'+esc(member['name'])+' ('+esc(member['role'])+')</span> <select name="role"><option value="member">Member</option><option value="owner">Owner</option><option value="remove">Remove</option></select> <button>Update member</button>',bid)
                body+='</details><details><summary>Replace or revoke permanent PSK</summary><p>Immediate replacement requires re-enrollment of old-key workers. Never distribute a replacement over a compromised band.</p>'
                body+=self.form(user,'rotate','<label><input type="checkbox" name="confirm" value="yes" required> I understand existing workers need re-enrollment.</label><button>Generate replacement five-word PSK</button>',bid)
                body+=self.form(user,'revoke','<label><input type="checkbox" name="confirm" value="yes" required> Revoke this band’s current key.</label><button>Revoke band PSK</button>',bid)+'</details>'
                body+='<details><summary>Enrolled device certificates</summary><p>Revocation blocks future configuration fetches. Legacy PSK mesh traffic still requires a band cutover.</p>'
                for device in self.devices.list(user['id'],bid):
                    body+='<p>'+esc(device['name'])+' · '+('active' if device['active'] else 'revoked')+'</p>'
                    if device['active']:
                        body+=self.form(user,'device_revoke','<input type="hidden" name="device_id" value="'+device['id']+'"><button>Revoke device certificate</button>',bid)
                body+='</details>'
            body+='</section>'
        body+='<section><h2>Create a band</h2>'+self.form(user,'band_create','<label>Band name <input name="name" maxlength="100" required></label><button>Create band with a five-word PSK</button>')+'</section>'
        return self.response('Your Rook account',body)

    async def action(self,request):
        user=self.require(request)
        try:
            data=await request.post()
            self.csrf(request,data,user)
            op=str(data.get('op',''))
            bid=str(data.get('band_id',''))
            uid=user['id']
            if op=='logout':
                self.store.logout(request.cookies.get(COOKIE,''))
                response=self.redirect('/account/login'); response.del_cookie(COOKIE); return response
            if op in ('local','password','unlink','merge'):
                self.require(request,fresh=True)
            if op=='local':
                await asyncio.to_thread(self.store.add_local,uid,data.get('username',''),data.get('password',''))
            elif op=='password':
                if uid==self.bootstrap_id:
                    raise ValueError('Change the operator password in the server configuration; this recovery login is managed there.')
                if await asyncio.to_thread(self.store.login,user['username'],data.get('current_password',''))!=uid:
                    raise ValueError('Current password did not match.')
                await asyncio.to_thread(self.store.set_password,uid,data.get('password',''))
                return self.signed_in(uid)
            elif op=='unlink':
                self.store.unlink_google(uid)
            elif op=='merge':
                grant=self.store.consume(str(data.get('grant','')),'merge')
                if grant['merge_to']!=uid:
                    raise PermissionError('Account merge mismatch.')
                self.store.merge(grant['merge_from'],uid)
                await self.refresh_avatar(uid,force=True)
                return self.signed_in(uid)
            elif op=='device_approve':
                self.store.device_login_approve(uid,str(data.get('user_code','')).strip())
            elif op=='device_revoke':
                self.devices.revoke(uid,str(data.get('device_id','')))
            elif op=='profile':
                name=str(data.get('name','')).strip()
                if not 1<=len(name)<=100:
                    raise ValueError('Display name must be 1–100 characters.')
                with self.store.db() as db:
                    db.execute('UPDATE users SET name=? WHERE id=?',(name,uid))
            elif op=='avatar':
                source=data.get('source','')
                if source not in ('google','initials'):
                    raise ValueError('Invalid avatar source.')
                self.store.set_avatar(uid,source)
                await self.refresh_avatar(uid,force=True)
            elif op=='avatar_upload':
                upload=data.get('image')
                if not isinstance(upload,web.FileField):
                    raise ValueError('Choose an image.')
                raw=upload.file.read(2*1024*1024+1)
                image=await asyncio.to_thread(normalize_avatar,raw)
                self.store.set_avatar(uid,'custom',image)
            elif op=='accept':
                self.store.accept_invite(uid,str(data.get('invite','')).strip())
            elif op=='band_create':
                if not self.store.rate_limit(uid,'band_create',5):
                    raise ValueError('Please wait before creating another band.')
                name=str(data.get('name','')).strip()
                if not 1<=len(name)<=100:
                    raise ValueError('Band name must be 1–100 characters.')
                band=self.server._enrollment.register(name,generate_psk(),self.server.hub_public)
                self.store.assign(band['id'],uid)
                await self.server._sync_enrollment()
            elif op in ('pair','pair_revoke','invite','member','rotate','revoke'):
                self.store.require_band(uid,bid,True)
                if op=='pair':
                    grant=self.server._enrollment.issue(bid)
                    url=self.origin+'/worker?band='+grant['code']
                    body='<p>Code <strong id="code">'+grant['code']+'</strong> · <span id="expires"></span></p><pre id="command">'+esc("curl -fsSL '"+url+"' | bash")+'</pre><p><a href="/account">Back to account</a></p>'
                    payload=json.dumps({'csrf':user['csrf'],'band_id':bid,'session':grant['session']}).replace('<','\\u003c')
                    origin=json.dumps(self.origin).replace('<','\\u003c')
                    body+='<script>let expires='+str(grant['expires'])+';const payload='+payload+';const origin='+origin+';let busy=false;setInterval(async()=>{const left=Math.ceil(expires-Date.now()/1000);document.getElementById("expires").textContent=Math.max(0,left)+" seconds remaining";if(left>0||busy)return;busy=true;try{const r=await fetch("/account/pairing",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});const d=await r.json();if(!r.ok)throw Error(d.error||"Pairing stopped");expires=d.expires;document.getElementById("code").textContent=d.code;document.getElementById("command").textContent="curl -fsSL " + String.fromCharCode(39)+origin+"/worker?band="+d.code+String.fromCharCode(39)+" | bash";busy=false;}catch(e){document.getElementById("code").textContent="Stopped";document.getElementById("expires").textContent=e.message;expires=Infinity;}},1000);</script>'
                    return self.response('Pair a worker',body)
                if op=='pair_revoke': self.server._enrollment.revoke_code(bid)
                elif op=='invite':
                    token=self.store.invite(uid,bid)
                    return self.response('Invite a member','<p>Share this single-use link. It expires in seven days.</p><pre>'+esc(self.origin+'/account?invite='+token)+'</pre><a href="/account">Back</a>')
                elif op=='member': self.store.change_member(uid,bid,str(data.get('user_id','')),str(data.get('role','')))
                elif op in ('rotate','revoke'):
                    if data.get('confirm')!='yes': raise ValueError('Confirm the credential change.')
                    if op=='rotate':
                        result=self.server._enrollment.rotate(bid)
                        await self.server._sync_enrollment()
                        return self.response('New permanent PSK','<pre>'+esc(result['psk'])+'</pre><p>Re-enroll devices using this key or a new pairing code.</p><a href="/account">Back</a>')
                    self.server._enrollment.revoke(bid)
                    await self.server._sync_enrollment()
            else:
                raise ValueError('Unknown action.')
            return self.redirect()
        except PermissionError as error:
            return self.response('Access denied','<p>'+esc(error)+'</p>',403)
        except (ValueError,TypeError) as error:
            return self.response('Unable to complete request','<p>'+esc(error)+'</p><a href="/account">Back to account</a>',400)
