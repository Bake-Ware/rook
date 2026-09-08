"""Google OIDC and bounded, provider-only avatar retrieval."""
import asyncio
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urljoin

import aiohttp
from aiohttp.resolver import ThreadedResolver
import jwt
from PIL import Image, ImageOps


class GoogleAuth:
    def __init__(self):
        path = os.environ.get('ROOK_GOOGLE_CLIENT_FILE', '')
        self.config = json.loads(Path(path).read_text())['web'] if path else {}
        self.client_id = self.config.get('client_id','')
        self.authorized_parties = {self.client_id}
        android_path = os.environ.get('ROOK_GOOGLE_ANDROID_CLIENT_FILE', '')
        if android_path:
            android = json.loads(Path(android_path).read_text())
            self.authorized_parties.add(android['installed']['client_id'])
        self.keys = {}
        self.keys_expire = 0
        self.lock = asyncio.Lock()

    @property
    def enabled(self):
        return bool(self.client_id and self.config.get('client_secret'))

    def authorize_url(self, state, nonce, verifier, redirect_uri):
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        return 'https://accounts.google.com/o/oauth2/v2/auth?'+urlencode({
            'client_id':self.client_id,'redirect_uri':redirect_uri,'response_type':'code',
            'scope':'openid email profile','state':state,'nonce':nonce,
            'code_challenge':challenge,'code_challenge_method':'S256','prompt':'select_account'})

    async def exchange(self, code, verifier, redirect_uri, nonce):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post('https://oauth2.googleapis.com/token',data={
                'code':code,'code_verifier':verifier,'client_id':self.client_id,
                'client_secret':self.config['client_secret'],'redirect_uri':redirect_uri,
                'grant_type':'authorization_code'},allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError('Google could not complete this sign-in. Please try again.')
                data=await response.json()
        return await self.verify(data.get('id_token',''),nonce)

    async def verify(self, token, nonce):
        if not self.enabled or not isinstance(token,str) or len(token)>20000:
            raise ValueError('Invalid Google identity token.')
        try:
            header=jwt.get_unverified_header(token)
            if header.get('alg') != 'RS256' or not isinstance(header.get('kid'),str):
                raise ValueError('Invalid Google signing key.')
            async with self.lock:
                if time.time()>=self.keys_expire or header['kid'] not in self.keys:
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                        async with session.get('https://www.googleapis.com/oauth2/v3/certs',allow_redirects=False) as response:
                            response.raise_for_status()
                            data=await response.json()
                    self.keys={k['kid']:jwt.algorithms.RSAAlgorithm.from_jwk(k) for k in data['keys'] if k.get('kty')=='RSA' and k.get('alg')=='RS256'}
                    self.keys_expire=time.time()+3600
            claims=jwt.decode(token,self.keys[header['kid']],algorithms=['RS256'],
                              audience=self.client_id,issuer=['https://accounts.google.com','accounts.google.com'],
                              options={'require':['exp','iat','sub','iss','aud','nonce']},leeway=30)
            if not nonce or not hmac.compare_digest(str(claims.get('nonce','')),nonce):
                raise ValueError('Google sign-in nonce did not match.')
            if not isinstance(claims['sub'],str) or not 1<=len(claims['sub'])<=255:
                raise ValueError('Invalid Google account identity.')
            if claims.get('azp',self.client_id) not in self.authorized_parties | {self.client_id}:
                raise ValueError('Invalid Google authorized client.')
            return claims
        except (jwt.PyJWTError,KeyError,TypeError) as error:
            raise ValueError('Invalid or expired Google identity token.') from error


class PublicResolver(ThreadedResolver):
    async def resolve(self, host, port=0, family=0):
        result=await super().resolve(host,port,family)
        if not result or any(not ipaddress.ip_address(r['host']).is_global for r in result):
            raise ValueError('Avatar address is not public.')
        return result


def avatar_url_ok(url):
    try:
        u=urlsplit(url)
        return (u.scheme=='https' and u.port in (None,443) and not u.username and not u.password
                and bool(re.fullmatch(r'lh[0-9]+\.googleusercontent\.com',u.hostname or '')))
    except ValueError:
        return False


def normalize_avatar(data):
    if not data or len(data)>2*1024*1024:
        raise ValueError('Image must be smaller than 2 MB.')
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in ('PNG','JPEG','WEBP','GIF') or source.width*source.height>4_000_000:
                raise ValueError('Unsupported or oversized image.')
            source.seek(0)
            image=ImageOps.exif_transpose(source).convert('RGB')
            image=ImageOps.fit(image,(256,256))
            out=io.BytesIO()
            image.save(out,'JPEG',quality=85)
            return out.getvalue()
    except (OSError,Image.DecompressionBombError) as error:
        raise ValueError('Invalid image.') from error


async def fetch_avatar(url):
    connector=aiohttp.TCPConnector(resolver=PublicResolver())
    async with aiohttp.ClientSession(connector=connector,timeout=aiohttp.ClientTimeout(total=8)) as session:
        for _ in range(3):
            if not avatar_url_ok(url):
                raise ValueError('Unsupported Google avatar URL.')
            async with session.get(url,allow_redirects=False) as response:
                if response.status in (301,302,303,307,308):
                    url=urljoin(url,response.headers.get('Location',''))
                    continue
                if response.status!=200:
                    raise ValueError('Google photo unavailable.')
                data=bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data)>2*1024*1024:
                        raise ValueError('Google photo too large.')
                return await asyncio.to_thread(normalize_avatar,bytes(data))
    raise ValueError('Too many image redirects.')
