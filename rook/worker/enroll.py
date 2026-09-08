"""Interactive, HTTPS-only config enrollment without a PSK on the command line."""
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request
import webbrowser


def storage_path():
    return Path(os.environ.get('ROOK_ENROLLMENT_FILE',str(Path.home()/'.rook-band-worker/enrollment.json'))).expanduser()


def post(server,path,data):
    url=urlsplit(server)
    if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('','/'):
        raise ValueError('Use the HTTPS origin of your Rook server.')
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
    fd,temp=tempfile.mkstemp(dir=path.parent,prefix='.enrollment-')
    try:
        with os.fdopen(fd,'w') as output:
            json.dump(data,output,indent=2);output.write('\n');output.flush();os.fsync(output.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def load():
    try:
        return json.loads(storage_path().read_text())
    except FileNotFoundError:
        return {}


def enroll(server,pair_code=None):
    key,csr=certificate_request()
    issued=None
    if pair_code:
        import socket
        issued=post(server,'/auth/devices/enroll',{'code':pair_code.strip().lower(),'csr':csr,'name':socket.gethostname()})
        band=issued['band']
        bands=[band]
    else:
        grant=post(server,'/auth/device/start',{})
        print('Open',grant['verification_uri_complete'])
        print('Verify this installer code:',grant['user_code'])
        print('Sign in with Google or your local account, then authorize this installer.')
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
            if error:raise ValueError('Installer authorization expired. Start again.')
            bands=result['bands'];break
        else:raise ValueError('Installer authorization expired.')
    if not bands:raise ValueError('No authorized bands. Create a band or accept an invitation in the web app first.')
    for index,band in enumerate(bands,1):
        print(f'{index}. {band["name"]}')
    chosen=0 if len(bands)==1 else int(input('Active band number: '))-1
    if chosen not in range(len(bands)):raise ValueError('Invalid band selection.')
    if issued is None:
        import socket
        issued=post(server,'/auth/devices/enroll',{'enrollment_grant':result['enrollment_grants'][bands[chosen]['id']],'csr':csr,'name':socket.gethostname()})
    from cryptography.hazmat.primitives import serialization
    issued['private_key']=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
    issued.pop('band',None)
    data={'server':server.rstrip('/'),'bands':bands,'active_band':bands[chosen]['id'],'device':issued,'last_verified':time.time()}
    save(data)
    print('Saved configurations privately to',storage_path())
    print('Start the worker with --enrolled (add --ws when using a port-443 hub).')


def certificate_request():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key=ec.generate_private_key(ec.SECP256R1())
    csr=x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'rook worker')])).sign(key,hashes.SHA256())
    return key,csr.public_bytes(serialization.Encoding.PEM).decode()


def proof(saved,purpose):
    import base64
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    device=saved['device']
    challenge=post(saved['server'],'/auth/devices/challenge',{'device_id':device['device_id'],'purpose':purpose})
    key=serialization.load_pem_private_key(device['private_key'].encode(),password=None)
    return {'challenge':challenge['challenge'],'certificate':device['certificate'],
            'signature':base64.b64encode(key.sign(challenge['message'].encode(),ec.ECDSA(hashes.SHA256()))).decode()}


def refresh():
    """Fetch current credentials with the device key; never fall back on denial."""
    saved=load()
    if not saved.get('device'):
        return saved
    import urllib.error
    from cryptography import x509
    import datetime
    try:
        result=post(saved['server'],'/auth/devices/config',proof(saved,'config'))
    except (urllib.error.URLError,TimeoutError,OSError) as error:
        if isinstance(error,urllib.error.HTTPError) and error.code != 429 and error.code < 500:
            raise ValueError('Device authorization was denied. Re-enroll through an owner.') from None
        # Explicit one-hour offline boot lease; known authorization denials never use it.
        cert=x509.load_pem_x509_certificate(saved['device']['certificate'].encode())
        age=time.time()-saved.get('last_verified',0)
        if 0<=age<3600 and cert.not_valid_after_utc>datetime.datetime.now(datetime.timezone.utc):
            return saved
        raise ValueError('Cannot verify device authorization; reconnect to the enrollment server.') from None
    band=result['band']
    previous=next((b for b in saved['bands'] if b['id']==saved['active_band']),None)
    if band['id']!=saved['active_band'] or (previous and band['epoch']<previous['epoch']):
        raise ValueError('Enrollment server returned an older credential epoch or a different band.')
    saved['bands']=[band if b['id']==band['id'] else b for b in saved['bands']]
    saved['last_verified']=time.time()
    cert=x509.load_pem_x509_certificate(saved['device']['certificate'].encode())
    if cert.not_valid_after_utc-datetime.datetime.now(datetime.timezone.utc)<datetime.timedelta(days=7):
        renewed=post(saved['server'],'/auth/devices/renew',proof(saved,'renew'))
        saved['device']['certificate']=renewed['certificate']
    save(saved)
    return saved
