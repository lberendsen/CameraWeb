# -*- coding: utf-8 -*-
"""Servidor local do CameraWeb.

Serve o index.html em http://localhost:8765/ e faz, em nome da página, os pedidos
que o navegador não pode fazer sozinho:

  /api/voos     aviões (bases abertas adsb.fi e adsb.lol, que não aceitam CORS)
  /api/youtube  câmeras ao vivo do YouTube, localizadas no mapa (YouTube Data API v3)
  /api/cet      câmeras de trânsito da CET-SP (lista lida do site cameras.cetsp.com.br)
  /api/navios   navios em tempo real (AIS) da área na tela, via AISStream.io (navios.py)
  /api/cidade   São Paulo agora: alagamentos (CGE), helicópteros, ônibus (cidade.py)
  /api/chave, /api/chave-youtube   guardam as chaves em arquivos desta pasta

Só atende a própria máquina (127.0.0.1).
"""
import html
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import cidade
import geo_titulo
import navios

PORTA = int(os.environ.get('CAMERAWEB_PORTA') or 8765)   # outra porta só para testes
PASTA = os.path.dirname(os.path.abspath(__file__))
FONTES = [
    'https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{dist}',
    'https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}',
]
ARQ_CHAVE = os.path.join(PASTA, 'chave-windy.txt')      # chave da Windy, entregue à página
ARQ_CHAVE_YT = os.path.join(PASTA, 'chave-youtube.txt')  # chave do Google; fica só no servidor
ARQ_CACHE_YT = os.path.join(PASTA, 'youtube-cache.json')
ARQ_CHAVE_NAVIOS = os.path.join(PASTA, 'chave-aisstream.txt')  # chave da AISStream (navios)
ARQ_TOKEN_SPTRANS = os.path.join(PASTA, 'chave-sptrans.txt')   # token da API Olho Vivo (ônibus)
CACHE_SEG = 5            # aviões: pedidos iguais em menos de 5 s reaproveitam a resposta
YT_COTA_DIA = 10000      # cota gratuita diária do Google; cada busca custa 100, cada lote de detalhes 1
cache = {}
trava = threading.Lock()


def ler(arquivo):
    try:
        with open(arquivo, encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def gravar(arquivo, texto):
    with open(arquivo, 'w', encoding='utf-8') as f:
        f.write(texto)


def baixar_json(url, timeout=8):
    req = urllib.request.Request(url, headers={'User-Agent': 'CameraWeb/1.0 (mapa pessoal)'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


# ---------- aviões ----------

def buscar_voos(lat, lon, dist):
    chave = (lat, lon, dist)
    with trava:
        hit = cache.get(chave)
        if hit and time.time() - hit[0] < CACHE_SEG:
            return hit[1]
    erro = None
    for modelo in FONTES:
        url = modelo.format(lat=lat, lon=lon, dist=dist)
        try:
            dados = baixar_json(url)
            avioes = dados.get('ac') or dados.get('aircraft') or []
            corpo = json.dumps({'fonte': url.split('/')[2], 'ac': avioes}).encode('utf-8')
            with trava:
                cache[chave] = (time.time(), corpo)
                for k in [k for k, v in cache.items() if time.time() - v[0] > 60]:
                    del cache[k]
            return corpo
        except Exception as e:  # tenta a próxima fonte
            erro = e
    raise erro or RuntimeError('nenhuma fonte de voos respondeu')


# ---------- YouTube ----------
# Buscar por localização traz missas e jogos, não câmeras. Por isso o servidor faz
# algumas buscas por palavras ("live cam", "câmera ao vivo"...) em vários idiomas,
# a cada 6 horas, e descobre o lugar de cada transmissão: pela localização cadastrada
# no vídeo ou, na falta dela, pelo título (geo_titulo.py). Tudo fica em disco.

YT_BUSCAS = [   # (palavras, região, idioma) — cada busca custa 100 unidades da cota
    ('câmera ao vivo|camera ao vivo|ao vivo 24h|praia ao vivo|live cam brasil', 'BR', 'pt'),
    ('live cam|webcam live|live webcam 24/7', 'US', 'en'),
    ('live cam|webcam live', 'GB', 'en'),
    ('cámara en vivo|webcam en vivo|en vivo 24/7', 'MX', 'es'),
    ('webcam en vivo|cámara en vivo', 'ES', 'es'),
    ('webcam em direto|câmara em direto', 'PT', 'pt'),
    ('webcam live|webcam in diretta', 'IT', 'it'),
    ('webcam live', 'DE', 'de'),
    ('webcam en direct', 'FR', 'fr'),
    ('ライブカメラ', 'JP', 'ja'),
    # aeroportos e portos: para ver na câmera o mesmo avião/navio que aparece no mapa
    ('airport live|plane spotting live|runway live cam', 'US', 'en'),
    ('aeroporto ao vivo|porto ao vivo|navios ao vivo|aviões ao vivo', 'BR', 'pt'),
    ('ship spotting live|port live cam|harbour live cam|cruise port live', 'GB', 'en'),
]
YT_RENOVAR_SEG = 6 * 3600
yt_estado = {'processando': False, 'erro': None}


def dia_google():
    # a cota do Google zera à meia-noite do horário do Pacífico (aprox. UTC-8)
    return time.strftime('%Y-%m-%d', time.gmtime(time.time() - 8 * 3600))


def carregar_cache_yt():
    try:
        with open(ARQ_CACHE_YT, encoding='utf-8') as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    if c.get('versao') != 2:              # cache do formato antigo (busca por área): descarta
        c = {'versao': 2, 'unidades': c.get('unidades', 0), 'dia': c.get('dia')}
    c.setdefault('videos', [])
    c.setdefault('geo', {})               # videoId -> [lat, lon, descrição] ou None (não achou)
    c.setdefault('t', 0)
    if c.get('dia') != dia_google():
        c['dia'], c['unidades'] = dia_google(), 0
    return c


def salvar_cache_yt(c):
    try:
        with open(ARQ_CACHE_YT, 'w', encoding='utf-8') as f:
            json.dump(c, f, ensure_ascii=False)
    except OSError:
        pass


def erro_google(e):
    try:
        motivo = json.loads(e.read().decode('utf-8'))['error']['errors'][0]['reason']
    except Exception:
        motivo = ''
    if motivo in ('quotaExceeded', 'dailyLimitExceeded', 'rateLimitExceeded'):
        return 'cota'
    if motivo in ('keyInvalid', 'badRequest') or e.code in (400, 401):
        return 'chave'
    if motivo in ('accessNotConfigured', 'forbidden'):
        return 'api-desativada'
    return 'google-%s' % e.code


def renovar_youtube():
    """Roda em segundo plano: busca as transmissões e localiza cada uma."""
    chave_api = ler(ARQ_CHAVE_YT)
    base = 'https://www.googleapis.com/youtube/v3/'
    try:
        achados = {}
        for palavras, regiao, idioma in YT_BUSCAS:
            with trava:
                c = carregar_cache_yt()
                if c['unidades'] + 101 > YT_COTA_DIA:
                    raise RuntimeError('cota')
                c['unidades'] += 100
                salvar_cache_yt(c)
            r = baixar_json(base + 'search?' + urlencode({
                'part': 'id', 'type': 'video', 'eventType': 'live', 'order': 'viewCount', 'maxResults': 50,
                'q': palavras, 'regionCode': regiao, 'relevanceLanguage': idioma, 'videoEmbeddable': 'true',
                'key': chave_api}), timeout=15)
            for i in r.get('items', []):
                vid = i.get('id', {}).get('videoId')
                if vid:
                    achados[vid] = True
        ids = list(achados)
        videos = []
        for k in range(0, len(ids), 50):  # detalhes: 1 unidade por lote de 50
            det = baixar_json(base + 'videos?' + urlencode({
                'part': 'snippet,recordingDetails,liveStreamingDetails', 'id': ','.join(ids[k:k + 50]),
                'key': chave_api}), timeout=15)
            with trava:
                c = carregar_cache_yt(); c['unidades'] += 1; salvar_cache_yt(c)
            for v in det.get('items', []):
                sn = v.get('snippet') or {}
                loc = (v.get('recordingDetails') or {}).get('location') or {}
                thumbs = sn.get('thumbnails') or {}
                videos.append({
                    'id': v['id'], 'titulo': html.unescape(sn.get('title', '')), 'canal': sn.get('channelTitle', ''),
                    'assistindo': int((v.get('liveStreamingDetails') or {}).get('concurrentViewers') or 0),
                    'thumb': (thumbs.get('medium') or thumbs.get('default') or {}).get('url', ''),
                    'gps': [loc['latitude'], loc['longitude']] if loc.get('latitude') is not None else None,
                })
        with trava:
            c = carregar_cache_yt()
            c['videos'], c['t'] = videos, time.time()
            salvar_cache_yt(c)
        localizar_pendentes()
        yt_estado['erro'] = None
    except urllib.error.HTTPError as e:
        yt_estado['erro'] = erro_google(e)
    except RuntimeError as e:
        yt_estado['erro'] = str(e)
    except Exception as e:
        yt_estado['erro'] = 'rede'
        sys.stderr.write('YouTube: %r\n' % e)
    finally:
        yt_estado['processando'] = False


def pendentes(c):
    return [v for v in c['videos'] if not v['gps'] and v['id'] not in c['geo']]


def localizar_pendentes():
    # localização pelo título, uma transmissão por vez (o resultado fica guardado para sempre)
    while True:
        with trava:
            falta = pendentes(carregar_cache_yt())
        if not falta:
            return
        v = falta[0]
        achou = geo_titulo.localizar(v['titulo'])
        with trava:
            c = carregar_cache_yt()
            c['geo'][v['id']] = list(achou) if achou else None
            salvar_cache_yt(c)


def so_localizar():
    try:
        localizar_pendentes()
    finally:
        yt_estado['processando'] = False


def listar_youtube():
    if not ler(ARQ_CHAVE_YT):
        return {'erro': 'sem-chave'}
    with trava:
        c = carregar_cache_yt()
        velho = time.time() - c['t'] > YT_RENOVAR_SEG
        if velho and not yt_estado['processando'] and yt_estado['erro'] not in ('chave', 'api-desativada'):
            yt_estado['processando'] = True
            threading.Thread(target=renovar_youtube, daemon=True).start()
        elif not yt_estado['processando'] and pendentes(c):
            # o servidor foi fechado no meio da localização: continua de onde parou
            yt_estado['processando'] = True
            threading.Thread(target=so_localizar, daemon=True).start()
        saida, sem_lugar = [], 0
        for v in c['videos']:
            if v['gps']:
                lat, lon, origem = v['gps'][0], v['gps'][1], 'gps'
            elif c['geo'].get(v['id']):
                lat, lon, origem = c['geo'][v['id']][0], c['geo'][v['id']][1], 'titulo'
            else:
                sem_lugar += 1
                continue
            d = dict(v, lat=lat, lon=lon, origem=origem)
            d.pop('gps', None)
            saida.append(d)
    return {'videos': saida, 'sem_lugar': sem_lugar, 'total': len(c['videos']),
            'processando': yt_estado['processando'], 'erro': yt_estado['erro'],
            'unidades': c['unidades'], 'cota': YT_COTA_DIA, 'atualizado': c['t']}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=PASTA, **kw)

    def end_headers(self):
        # arquivos da página: o navegador sempre confere se há versão nova (senão mostra a antiga após uma atualização)
        if not self.path.startswith('/api/'):
            self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def responder(self, codigo, corpo, tipo='application/json; charset=utf-8'):
        if not isinstance(corpo, bytes):
            corpo = json.dumps(corpo, ensure_ascii=False).encode('utf-8')
        self.send_response(codigo)
        self.send_header('Content-Type', tipo)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_POST(self):
        if urlparse(self.path).path == '/api/desligar':
            # um servidor mais novo (aberto pelo .bat) pediu a vez
            self.responder(204, b'')
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        # a página manda a chave quando você a cola; fica salva para as próximas vezes
        arquivos = {'/api/chave': ARQ_CHAVE, '/api/chave-youtube': ARQ_CHAVE_YT, '/api/chave-navios': ARQ_CHAVE_NAVIOS,
                    '/api/chave-sptrans': ARQ_TOKEN_SPTRANS}
        arquivo = arquivos.get(urlparse(self.path).path)
        if not arquivo:
            return self.send_error(404)
        n = min(int(self.headers.get('Content-Length') or 0), 1000)
        gravar(arquivo, self.rfile.read(n).decode('utf-8').strip())
        if arquivo == ARQ_CHAVE_YT:
            yt_estado['erro'] = None          # chave nova: tenta buscar de novo
        if arquivo == ARQ_CHAVE_NAVIOS:
            frota.chave_nova()
        if arquivo == ARQ_TOKEN_SPTRANS:
            sp_agora.cookie = None            # entra de novo com o token novo
        self.responder(204, b'')

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == '/api/chave':
            return self.responder(200, ler(ARQ_CHAVE).encode('utf-8'), 'text/plain; charset=utf-8')
        if u.path == '/api/chave-youtube':   # só diz se existe; a chave do Google não sai do servidor
            return self.responder(200, {'tem': bool(ler(ARQ_CHAVE_YT))})
        if u.path == '/api/youtube':
            return self.responder(200, listar_youtube())
        if u.path == '/api/navios':
            try:
                s_, w_, n_, e_ = (float(q[k][0]) for k in ('s', 'w', 'n', 'e'))
            except (KeyError, ValueError):
                return self.send_error(400, 'parametros: s, w, n, e')
            return self.responder(200, frota.consultar(s_, w_, n_, e_))
        if u.path == '/api/cidade':
            return self.responder(200, sp_agora.consultar())
        if u.path == '/api/chave-sptrans':
            return self.responder(200, {'tem': bool(ler(ARQ_TOKEN_SPTRANS))})
        if u.path == '/api/chave-navios':
            return self.responder(200, {'tem': bool(ler(ARQ_CHAVE_NAVIOS))})
        if u.path == '/api/cet':
            try:
                return self.responder(200, {'cameras': listar_cet()})
            except Exception as e:
                return self.responder(502, {'erro': str(e)})
        if u.path != '/api/voos':
            return super().do_GET()
        try:
            lat = round(float(q['lat'][0]), 2)
            lon = round(float(q['lon'][0]), 2)
            dist = max(1, min(int(float(q['dist'][0])), 250))   # as bases aceitam até 250 milhas náuticas
        except (KeyError, ValueError):
            return self.send_error(400, 'parametros: lat, lon, dist')
        try:
            self.responder(200, buscar_voos(lat, lon, dist))
        except Exception as e:
            self.responder(502, {'erro': str(e)})

    def log_message(self, fmt, *args):  # silencioso, exceto erros
        if args and str(args[1])[:1] in '45':
            sys.stderr.write('%s\n' % (fmt % args))


# ---------- câmeras da CET-SP ----------
# O site cameras.cetsp.com.br traz a lista de câmeras dentro da página (gCams.push(...)),
# sem coordenadas. As posições abaixo são os cruzamentos achados no OpenStreetMap.
# Câmera nova que a CET publicar: tenta achar a rua no Photon (posição aproximada).

CET_PAGINA = 'https://cameras.cetsp.com.br/View/Cam.aspx'
CET_IMAGENS = 'https://cameras.cetsp.com.br/Cams/'
CET_POSICOES = {   # pasta -> (lat, lon)
    225: (-23.597729, -46.650830),   # Av Ascendino Reis x R Pedro de Toledo
    184: (-23.579142, -46.661583),   # Av Brasil x Av Brig Luís Antônio
    195: (-23.564412, -46.678096),   # Av Brasil x Av Henrique Schaumann / Av Rebouças
    210: (-23.568646, -46.649885),   # Av Brig Luís Antônio x Al Santos
    220: (-23.580329, -46.683471),   # Av Cidade Jardim x Av Nove de Julho
    180: (-23.547935, -46.649033),   # R da Consolação x R Caio Prado
    222: (-23.597448, -46.667934),   # Av Hélio Pellegrino x R Diogo Jácome
    224: (-23.593650, -46.652770),   # Av Ibirapuera x R Ipê (aprox.)
    200: (-23.582099, -46.684128),   # Av Brig Faria Lima x R Jerônimo da Veiga
    23: (-23.567652, -46.649020),    # Av Paulista x Av Brig Luís Antônio
    22: (-23.558111, -46.660192),    # Av Paulista x R Augusta (Metrô Consolação)
}
cet_cache = {'t': 0, 'lista': []}
frota = navios.Navios(lambda: ler(ARQ_CHAVE_NAVIOS))
sp_agora = cidade.Cidade(PASTA, lambda: ler(ARQ_TOKEN_SPTRANS))
cet_extra = {}   # posições achadas pelo Photon para câmeras novas


def listar_cet():
    with trava:
        if time.time() - cet_cache['t'] < 1800:
            return cet_cache['lista']
    req = urllib.request.Request(CET_PAGINA, headers={'User-Agent': 'Mozilla/5.0 (CameraWeb)'})
    with urllib.request.urlopen(req, timeout=15) as r:
        pagina = r.read().decode('utf-8', 'replace')
    lista = []
    for m in re.finditer(r'gCams\.push\(\{(.*?)\}\)', pagina):
        campos = dict(re.findall(r"(\w+):('(?:[^'\\]|\\.)*'|[^,]+)", m.group(1)))
        def txt(k):
            return campos.get(k, "''").strip("'")
        try:
            pasta = int(campos['pasta'])
        except (KeyError, ValueError):
            continue
        pos = CET_POSICOES.get(pasta) or cet_extra.get(pasta)
        aprox = pasta not in CET_POSICOES
        if not pos and pasta not in cet_extra:
            achou = None
            try:
                q = urllib.parse.quote('%s, São Paulo' % txt('subTitulo'))
                f = baixar_json('https://photon.komoot.io/api/?limit=1&q=' + q).get('features', [])
                if f and f[0]['properties'].get('city') == 'São Paulo':
                    lon, lat = f[0]['geometry']['coordinates']
                    achou = (lat, lon)
            except Exception:
                pass
            cet_extra[pasta] = pos = achou
        if not pos:
            continue
        lista.append({
            'pasta': pasta, 'titulo': txt('titulo'), 'sub': txt('subTitulo'), 'detalhe': txt('detalhe'),
            'ativa': campos.get('ativa', 'true').strip() == 'true',
            'n': int(campos.get('qtdeImagens', '25') or 25), 'lat': pos[0], 'lon': pos[1], 'aprox': aprox,
            'base': CET_IMAGENS + str(pasta) + '/',
        })
    with trava:
        cet_cache['t'], cet_cache['lista'] = time.time(), lista
    return lista


class Servidor(ThreadingHTTPServer):
    # No Windows, a opção padrão (SO_REUSEADDR) deixa dois servidores abrirem a mesma
    # porta ao mesmo tempo, e o navegador acaba falando com o antigo. Exigimos exclusividade.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    endereco = 'http://localhost:%d/' % PORTA
    srv = None
    for tentativa in range(10):
        try:
            srv = Servidor(('127.0.0.1', PORTA), Handler)
            break
        except OSError:
            # já tem um CameraWeb aberto (talvez de uma versão anterior): pede para ele sair
            if tentativa == 0:
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        'http://127.0.0.1:%d/api/desligar' % PORTA, data=b'', method='POST'), timeout=3)
                except Exception:
                    pass
            time.sleep(0.5)
    if srv is None:
        print('A porta %d continua ocupada. Feche as outras janelas do CameraWeb e tente de novo.' % PORTA)
        webbrowser.open(endereco)
        time.sleep(5)
        return
    print('CameraWeb rodando em %s' % endereco)
    print('Feche esta janela para desligar.')
    if '--sem-navegador' not in sys.argv:
        webbrowser.open(endereco)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
