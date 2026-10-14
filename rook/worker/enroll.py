"""Interactive config enrollment without a PSK on the command line.

HTTPS, except a hub addressed by a non-public (LAN, loopback, tailnet) IP literal
(a LAN quickstart hub serves its dashboard over plain HTTP; the installer that
runs this already came over the same connection)."""
import ipaddress
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from urllib.request import Request
import webbrowser

from ..core.paths import worker_state_path
from ..core.db import atomic_write_text


def storage_path():
    return Path(os.environ.get('ROOK_ENROLLMENT_FILE',str(worker_state_path('enrollment.json')))).expanduser()


def _lan_host(host):
    try:
        ip=ipaddress.ip_address(host)
    except ValueError:
        return host=='localhost'
    # Not globally routable: RFC 1918, loopback, link-local, CGNAT/tailnet.
    return not ip.is_global and not ip.is_multicast and not ip.is_unspecified


def check_origin(server,message='Use the HTTPS origin of your Rook server.'):
    """Raise unless ``server`` is a bare origin this worker may enroll with:
    ``https://host[:port]``, or ``http://`` to a non-public IP literal. A
    public name never gets plain HTTP, so a public hub stays HTTPS-only."""
    url=urlsplit(server)
    ok_scheme=url.scheme=='https' or (url.scheme=='http' and url.hostname is not None and _lan_host(url.hostname))
    if not ok_scheme or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('','/'):
        raise ValueError(message)


def post(server,path,data):
    check_origin(server)
    request=Request(server.rstrip('/')+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json','User-Agent':'rook-enrollment/1'})
    # Do not forward enrollment grants through HTTP redirects.
    import urllib.request
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as response:
        raw=response.read(2*1024*1024+1)
        if len(raw)>2*1024*1024:raise ValueError('Enrollment response too large.')
        return json.loads(raw)


def save(data,path=None):
    path=Path(path or storage_path())
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    atomic_write_text(path,json.dumps(data,indent=2)+'\n',0o600)


def load():
    try:
        return json.loads(storage_path().read_text())
    except FileNotFoundError:
        return {}


class Steps:
    """Enrollment progress as numbered, plain steps.

    ``nested`` (the installers set ``ROOK_INSTALLER=1``): the installer
    numbers its own steps and prints the final line, so only what the
    person must act on (the sign-in link, a band choice) is shown,
    indented under the installer's step."""

    def __init__(self, out=None, nested=None):
        import sys
        self.out = out or sys.stdout
        self.nested = (os.environ.get('ROOK_INSTALLER') == '1') if nested is None else nested
        self.n = 0

    def _print(self, text):
        print(text, file=self.out, flush=True)

    def title(self, text):
        if not self.nested:
            self._print(text)

    def step(self, text):
        self.n += 1
        if not self.nested:
            self._print(f'  {self.n}. {text}')

    def act(self, text):
        """A line the person needs, shown in both modes."""
        self._print(('[rook]       ' if self.nested else '     ') + text)

    def done(self, text):
        if not self.nested:
            self._print(text)


def interactive():
    """Prompts only with a person at a terminal, never with --yes/ROOK_YES."""
    import sys
    if os.environ.get('ROOK_YES', '').strip().lower() in ('1', 'true', 'yes'):
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def choose_band(bands, wanted=None, ask=None):
    """The band to join: the only one, the one named ``wanted`` (name or id,
    also ROOK_BAND), or one picked at a prompt when ``ask`` can prompt.
    Never prompts without a terminal: says how to choose instead."""
    wanted = (wanted or os.environ.get('ROOK_BAND', '')).strip()
    if wanted:
        hits = [b for b in bands if wanted in (b.get('id'), b.get('name'))]
        if len(hits) != 1:
            hits = [b for b in bands if str(b.get('name', '')).casefold() == wanted.casefold()]
        if len(hits) == 1:
            return hits[0]
        raise ValueError(f'No band called {wanted!r}. Your bands: '
                         + ', '.join(b['name'] for b in bands) + '.')
    if len(bands) == 1:
        return bands[0]
    names = ', '.join(b['name'] for b in bands)
    if ask is None:
        raise ValueError(f'Your account has {len(bands)} bands ({names}). Choose one with '
                         '--band NAME (or ROOK_BAND=NAME) and run this again.')
    for index, band in enumerate(bands, 1):
        ask.act(f'{index}) {band["name"]}')
    try:
        chosen = int(input(('[rook]       ' if ask.nested else '     ') + 'Band number: ')) - 1
    except (ValueError, EOFError):
        chosen = -1
    if chosen not in range(len(bands)):
        raise ValueError('Invalid band selection.')
    return bands[chosen]


def enroll(server,pair_code=None,name=None,band=None,out=None):
    """Join a band: with a pairing code, or by approving this machine in a
    browser signed in to the hub. Prints numbered steps and returns
    ``{'worker', 'band'}``. Prompts only at a terminal (:func:`interactive`)."""
    import socket
    steps=Steps(out)
    name=(name or socket.gethostname() or 'worker').strip()
    check_origin(server)
    if pair_code:
        steps.title(f'Joining a band with pairing code {pair_code.strip().lower()}')
    else:
        steps.title(f'Joining a band through your account on {server.rstrip("/")}')
    key,csr=certificate_request()
    steps.step('Made a private key for this machine')
    issued=None
    if pair_code:
        issued=post(server,'/auth/devices/enroll',{'code':pair_code.strip().lower(),'csr':csr,'name':name})
        bands=[issued['band']]
        chosen=bands[0]
        steps.step(f'The hub accepted the code: band "{chosen["name"]}"')
    else:
        grant=post(server,'/auth/device/start',{})
        steps.step('Approve this machine in a browser signed in to the hub:')
        steps.act(grant['verification_uri_complete'])
        steps.act(f'Check that the page shows the code {grant["user_code"]}.')
        try:webbrowser.open(grant['verification_uri_complete'])
        except Exception:pass
        deadline=time.monotonic()+min(grant['expires_in'],600)
        interval=max(grant['interval'],5)
        while time.monotonic()<deadline:
            time.sleep(interval)
            result=post(server,'/auth/device/poll',{'device_code':grant['device_code']})
            error=result.get('error')
            if error=='authorization_pending':continue
            if error=='slow_down':interval+=5;continue
            if error:raise ValueError('The approval expired. Run the same command again.')
            bands=result['bands'];break
        else:raise ValueError('The approval expired. Run the same command again.')
        if not bands:raise ValueError('Your account has no bands yet. Create one on the dashboard, then run this again.')
        chosen=choose_band(bands,band,steps if interactive() else None)
        steps.step(f'Approved. Band: "{chosen["name"]}"')
        issued=post(server,'/auth/devices/enroll',{'enrollment_grant':result['enrollment_grants'][chosen['id']],'csr':csr,'name':name})
    from .device_key import private_pem
    issued['private_key']=private_pem(key)
    issued.pop('band',None)
    # The plugins the hub wants on or off here (the pairing code's profile, or
    # the band's defaults); the worker applies it once, on its next start
    # (rook.worker.admin.apply_enrollment_profile).
    profile=issued.pop('plugin_profile',None)
    data={'server':server.rstrip('/'),'bands':bands,'active_band':chosen['id'],'device':issued,'last_verified':time.time()}
    if isinstance(profile,dict):data['plugin_profile']=profile
    save(data)
    steps.step('Saved the band settings where only you can read them')
    steps.done(f'Done: {name} joined {chosen["name"]}')
    return {'worker':name,'band':chosen['name']}


def active_band_name(saved=None):
    """The enrolled band's name, or '' (the installers print it at the end)."""
    saved=load() if saved is None else saved
    return next((b.get('name','') for b in saved.get('bands',[]) if b.get('id')==saved.get('active_band')),'')


def plain_error(error: BaseException) -> str:
    """An enrollment failure in words a person can act on."""
    import urllib.error
    if isinstance(error, urllib.error.HTTPError):
        if error.code in (400, 403):
            return ("the hub refused it. Pairing codes work once and expire after "
                    "5 minutes: get a fresh code on the dashboard's Install page.")
        if error.code == 429:
            return "too many tries. Wait a minute, then try again."
        return f"the hub answered with an error (HTTP {error.code})."
    if isinstance(error, urllib.error.URLError):
        return (f"could not reach the hub ({getattr(error, 'reason', error)}). Check the "
                "address and that this machine can reach it.")
    return str(error)


def certificate_request():
    from .device_key import certificate_request as create
    return create()


def proof(saved,purpose):
    import base64
    from .device_key import sign
    device=saved['device']
    challenge=post(saved['server'],'/auth/devices/challenge',{'device_id':device['device_id'],'purpose':purpose})
    return {'challenge':challenge['challenge'],'certificate':device['certificate'],
            'signature':base64.b64encode(sign(device['private_key'],challenge['message'].encode())).decode()}


def expires_at(device):
    if 'expires_at' in device:
        return float(device['expires_at'])
    from cryptography import x509
    return x509.load_pem_x509_certificate(device['certificate'].encode()).not_valid_after_utc.timestamp()


def refresh(want=(),on_result=None):
    """Fetch current credentials with the device key; never fall back on denial.

    ``want`` names optional extras the hub may attach to the same config
    response (``'workers'``: the account's band rosters), and ``on_result``
    receives that response; extras are never saved with the credentials.
    """
    saved=load()
    if not saved.get('device'):
        return saved
    import urllib.error
    try:
        body=proof(saved,'config')
        if want:body['want']=list(want)
        result=post(saved['server'],'/auth/devices/config',body)
    except (urllib.error.URLError,TimeoutError,OSError) as error:
        if isinstance(error,urllib.error.HTTPError) and error.code != 429 and error.code < 500:
            raise ValueError('Device authorization was denied. Re-enroll through an owner.') from None
        # Explicit one-hour offline boot lease; known authorization denials never use it.
        age=time.time()-saved.get('last_verified',0)
        if 0<=age<3600 and expires_at(saved['device'])>time.time():
            return saved
        raise ValueError('Cannot verify device authorization; reconnect to the enrollment server.') from None
    band=result['band']
    previous=next((b for b in saved['bands'] if b['id']==saved['active_band']),None)
    migration=result.get('migration') or {}
    moving=(migration.get('kind')=='move' and migration.get('phase')=='active'
            and migration.get('source_band_id')==saved['active_band']
            and migration.get('band')==band)
    if not moving and (band['id']!=saved['active_band'] or (previous and band['epoch']<previous['epoch'])):
        raise ValueError('Enrollment server returned an older credential epoch or a different band.')
    if moving:
        saved['active_band']=band['id']
        saved['device']['band_id']=band['id']
        saved['bands']=[band]
    else:
        saved['bands']=[band if b['id']==band['id'] else b for b in saved['bands']]
    if on_result is not None:
        try:on_result(result)
        except Exception:pass
    saved['last_verified']=time.time()
    saved['migration']=result.get('migration')
    if expires_at(saved['device'])-time.time()<7*86400:
        renewed=post(saved['server'],'/auth/devices/renew',proof(saved,'renew'))
        saved['device']['certificate']=renewed['certificate']
        saved['device']['expires_at']=renewed['expires_at']
    save(saved)
    if saved.get('migration',{} ) and saved['migration']['phase']=='prepared':
        mid=saved['migration']['id']
        acknowledgement=proof(saved,'stage:'+mid)
        acknowledgement['migration_id']=mid
        post(saved['server'],'/auth/devices/staged',acknowledgement)
    return saved
