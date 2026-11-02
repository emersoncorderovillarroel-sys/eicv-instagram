"""Publicador EICV. Los tokens solo se leen del entorno y nunca se imprimen."""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler, urlopen
from urllib.error import HTTPError, URLError


class SinRedireccion(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ClienteMeta:
    """HTTP estándar sin redirecciones para mantener el token en Meta."""
    def __init__(self):
        self.headers = {}
        self.opener = build_opener(SinRedireccion)

    def request(self, method, url, params=None, data=None, timeout=45):
        if params:
            url += "?" + urlencode(params)
        payload = urlencode(data).encode() if data is not None else None
        req = Request(url, data=payload, headers=self.headers, method=method)
        try:
            response = self.opener.open(req, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            return RespuestaMeta(response.status, json.loads(response.read()))


class RespuestaMeta:
    def __init__(self, status, body):
        self.status_code, self.body = status, body
        self.ok = 200 <= status < 300

    def json(self):
        return self.body

ROOT = Path(__file__).resolve().parent


class ErrorSeguro(Exception):
    pass


def escribir_estado(state, root=ROOT):
    path = root / 'estado.json'
    tmp = root / 'estado.tmp'
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)
    # Persistir ANTES de enviar una publicación evita repetirla si el proceso cae.
    for args in ([ 'git', 'add', 'estado.json' ],
                 [ 'git', 'commit', '-m', 'Actualizar registro de publicaciones EICV' ],
                 [ 'git', 'push', 'origin', 'HEAD:main' ]):
        result = subprocess.run(args, cwd=root, capture_output=True, text=True)
        if result.returncode:
            raise ErrorSeguro('No se pudo guardar el registro en GitHub. Se detiene el envío.')


class Instagram:
    def __init__(self, token, uid, expected, version='v26.0', api_base='auto', session=None):
        self.uid = uid
        self.expected = expected.lstrip('@').lower()
        self.version = version
        self.session = session or ClienteMeta()
        self.session.headers.update({'Authorization': 'Bearer ' + token})
        allowed = ('https://graph.instagram.com', 'https://graph.facebook.com')
        if api_base == 'auto':
            self.candidates = allowed
        elif api_base.rstrip('/') in allowed:
            self.candidates = (api_base.rstrip('/'),)
        else:
            raise ErrorSeguro('El dominio de la API debe ser uno de los dos dominios oficiales de Meta.')
        self.base = None

    def solicitar(self, method, path, data=None):
        try:
            r = self.session.request(method, f'{self.base}/{self.version}/{path}',
                                     params=data if method == 'GET' else None,
                                     data=data if method == 'POST' else None, timeout=45)
            body = r.json()
        except (URLError, OSError, ValueError):
            raise ErrorSeguro('No se pudo confirmar la respuesta de Meta; no se reintentará un envío automáticamente.') from None
        if not isinstance(body, dict):
            raise ErrorSeguro('La respuesta de Meta no tiene el formato esperado.')
        if not r.ok or 'error' in body:
            err = body.get('error', {})
            # No imprimir respuestas completas: pueden incluir información sensible.
            raise ErrorSeguro(f'Meta rechazó la operación: HTTP {r.status_code}, código {err.get("code", "sin código")}, subcódigo {err.get("error_subcode", "sin subcódigo")}.')
        return body

    def comprobar_cuenta(self):
        last_error = None
        for base in self.candidates:
            self.base = base
            try:
                profile = self.solicitar('GET', self.uid, {'fields': 'id,username'})
            except ErrorSeguro as error:
                last_error = error
                continue
            if str(profile.get('username', '')).lower() != self.expected:
                raise ErrorSeguro('El token no corresponde al perfil esperado. No se publica.')
            print(f'Cuenta confirmada: @{self.expected}; API: {self.base}')
            return profile
        raise last_error or ErrorSeguro('No fue posible comprobar la cuenta de Instagram.')


def comprobar_imagen(base, index):
    parsed = urlparse(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ErrorSeguro('PUBLIC_BASE_URL debe ser una URL HTTPS pública, sin credenciales.')
    url = f'{base.rstrip("/")}/posts/{index + 1:02d}.jpg'
    try:
        with urlopen(url, timeout=30) as response:
            if response.status != 200 or not response.headers.get('Content-Type', '').lower().startswith('image/jpeg') or not response.read(2).startswith(b'\xff\xd8'):
                raise ErrorSeguro('La imagen pública no está disponible como JPEG. Revisa GitHub Pages.')
    except (URLError, OSError):
        raise ErrorSeguro('No se pudo descargar la imagen pública. No se publica.') from None
    return url


def ejecutar(mode, root=ROOT, api=None, persist=escribir_estado, image_check=comprobar_imagen):
    posts = json.loads((root / 'calendario.json').read_text(encoding='utf-8'))
    state = json.loads((root / 'estado.json').read_text(encoding='utf-8'))
    idx = state['next_index']
    if not isinstance(idx, int) or idx < 0 or idx > len(posts):
        raise ErrorSeguro('El índice del calendario no es válido.')
    if idx == len(posts):
        print('Las 12 publicaciones están completadas. El calendario no se repite.')
        return
    pending = state.get('pending')
    if pending and pending.get('phase') == 'publishing':
        raise ErrorSeguro('Hay un envío pendiente de conciliación. Comprueba Instagram antes de reintentar; se bloquea para evitar duplicados.')
    if pending and pending.get('index') != idx:
        raise ErrorSeguro('El registro pendiente no coincide con el calendario.')
    if api is None:
        required = ('IG_ACCESS_TOKEN', 'IG_USER_ID', 'PUBLIC_BASE_URL')
        missing = [key for key in required if not os.getenv(key)]
        if missing:
            raise ErrorSeguro('Falta configuración: ' + ', '.join(missing))
        api = Instagram(os.environ['IG_ACCESS_TOKEN'], os.environ['IG_USER_ID'],
                        os.getenv('IG_USERNAME', 'eicvconsultores'),
                        os.getenv('IG_API_VERSION', 'v26.0'), os.getenv('IG_API_BASE', 'auto'))
    api.comprobar_cuenta()
    url = image_check(os.environ.get('PUBLIC_BASE_URL', ''), idx)
    caption = posts[idx]['caption']
    if not caption.strip() or len(caption) > 2200:
        raise ErrorSeguro('El texto de la publicación no es válido.')
    print(f'Pieza {idx + 1}/{len(posts)}: {posts[idx]["title"]}')
    if mode == 'validar':
        print('Validación correcta. No se creó ni publicó ningún contenido en Instagram.')
        return
    if pending:
        cid = pending['container_id']
    else:
        cid = api.solicitar('POST', f'{api.uid}/media', {'image_url': url, 'caption': caption})['id']
        state['pending'] = {'index': idx, 'container_id': cid, 'phase': 'prepared'}
        persist(state, root)
    for attempt in range(15):
        status = api.solicitar('GET', cid, {'fields': 'status_code'}).get('status_code')
        if status == 'FINISHED':
            break
        if status in ('ERROR', 'EXPIRED', 'PUBLISHED'):
            raise ErrorSeguro('El contenedor requiere revisión: ' + status)
        if attempt < 14:
            time.sleep(4)
    else:
        raise ErrorSeguro('Meta todavía no terminó de procesar la imagen. Se conservará el contenedor para comprobarlo después.')
    state['pending']['phase'] = 'publishing'
    persist(state, root)
    media = api.solicitar('POST', f'{api.uid}/media_publish', {'creation_id': cid})
    if not media.get('id'):
        raise ErrorSeguro('Meta no confirmó el identificador de publicación. Revisa Instagram antes de reintentar.')
    state['next_index'] = idx + 1
    state.setdefault('published', []).append({'item': idx + 1, 'media_id': media['id'], 'published_at': datetime.now(timezone.utc).isoformat()})
    del state['pending']
    persist(state, root)
    print('Publicación confirmada por Meta. Registro guardado en GitHub.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--modo', choices=('validar', 'publicar'), default='validar')
    try:
        ejecutar(parser.parse_args().modo)
    except ErrorSeguro as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('Error inesperado. Se detiene el proceso; revisa el registro antes de reintentar.', file=sys.stderr)
        sys.exit(1)
