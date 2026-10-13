# -*- coding: utf-8 -*-
"""Descobre onde fica uma transmissão do YouTube a partir do título.

Poucas transmissões têm localização cadastrada; a maioria diz o lugar no título
("PRAIA DE PONTA NEGRA AO VIVO! NATAL-RN / BRASIL"). Aqui o título é limpo, partido
em pedaços e cada pedaço é procurado no Photon (OpenStreetMap). Um resultado só é
aceito se:
  1. o nome do lugar achado está escrito no título, e
  2. o estado ou o país dele também aparece no título (por nome ou sigla), ou o
     resultado é o próprio estado/país citado.
Preferimos deixar uma transmissão fora do mapa a colocá-la no lugar errado.
"""
import html
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request

LIXO = re.compile(
    r"\b(live\s*stream(?:ing)?|live\s*cams?|livecam|web\s*cams?|webcam|cams?|cameras?|câmeras?|cámaras?|camaras?|"
    r"c[aâá]mera de rua|ao vivo|en vivo|em direto|en directo|en direct|in diretta|diretta|live|stream(?:ing)?|"
    r"24\s*/\s*7|24\s*h(?:s|oras)?|hd|4k|uhd|view|views|vista|panor[aâá]mica|you control the cams|"
    r"transmisi[oó]n|transmiss[aã]o|watch|now|agora|hoje|today|cam\s*\d+|madrugada|manh[aã]|tarde|noite)\b", re.I)
TIPOS = {'city', 'town', 'village', 'suburb', 'neighbourhood', 'locality', 'island', 'beach', 'volcano', 'peak',
         'aerodrome', 'bay', 'hamlet', 'district', 'municipality', 'borough', 'quarter', 'state', 'country'}
GENERICO = {'city', 'beach', 'praia', 'centro', 'main', 'plaza', 'clima', 'weather', 'trem', 'train', 'airport',
            'hurricane', 'storm', 'de rua', 'rua', 'street', 'downtown', 'port', 'harbor', 'harbour', 'live'}
# siglas que costumam aparecer nos títulos (UF do Brasil, estados dos EUA, países)
SIGLAS = {
    'acre': 'ac', 'alagoas': 'al', 'amapa': 'ap', 'amazonas': 'am', 'bahia': 'ba', 'ceara': 'ce',
    'distrito federal': 'df', 'espirito santo': 'es', 'goias': 'go', 'maranhao': 'ma', 'mato grosso': 'mt',
    'mato grosso do sul': 'ms', 'minas gerais': 'mg', 'para': 'pa', 'paraiba': 'pb', 'parana': 'pr',
    'pernambuco': 'pe', 'piaui': 'pi', 'rio de janeiro': 'rj', 'rio grande do norte': 'rn',
    'rio grande do sul': 'rs', 'rondonia': 'ro', 'roraima': 'rr', 'santa catarina': 'sc', 'sao paulo': 'sp',
    'sergipe': 'se', 'tocantins': 'to',
    'alabama': 'al', 'alaska': 'ak', 'arizona': 'az', 'arkansas': 'ar', 'california': 'ca', 'colorado': 'co',
    'connecticut': 'ct', 'delaware': 'de', 'florida': 'fl', 'georgia': 'ga', 'hawaii': 'hi', 'idaho': 'id',
    'illinois': 'il', 'indiana': 'in', 'iowa': 'ia', 'kansas': 'ks', 'kentucky': 'ky', 'louisiana': 'la',
    'maine': 'me', 'maryland': 'md', 'massachusetts': 'ma', 'michigan': 'mi', 'minnesota': 'mn',
    'mississippi': 'ms', 'missouri': 'mo', 'montana': 'mt', 'nebraska': 'ne', 'nevada': 'nv',
    'new hampshire': 'nh', 'new jersey': 'nj', 'new mexico': 'nm', 'new york': 'ny', 'north carolina': 'nc',
    'north dakota': 'nd', 'ohio': 'oh', 'oklahoma': 'ok', 'oregon': 'or', 'pennsylvania': 'pa',
    'rhode island': 'ri', 'south carolina': 'sc', 'south dakota': 'sd', 'tennessee': 'tn', 'texas': 'tx',
    'utah': 'ut', 'vermont': 'vt', 'virginia': 'va', 'washington': 'wa', 'west virginia': 'wv',
    'wisconsin': 'wi', 'wyoming': 'wy',
}
PAISES = {  # código -> nomes como aparecem em títulos
    'br': ['brasil', 'brazil'], 'us': ['usa', 'united states', 'eua', 'estados unidos', 'america'],
    'mx': ['mexico'], 'ar': ['argentina', 'patagonia'], 'cl': ['chile'], 'pe': ['peru'], 'co': ['colombia'],
    'uy': ['uruguay', 'uruguai'], 'pt': ['portugal'], 'es': ['espana', 'spain', 'espanha'],
    'fr': ['france', 'franca'], 'it': ['italia', 'italy'], 'de': ['deutschland', 'germany', 'alemanha'],
    'gb': ['uk', 'england', 'scotland', 'wales', 'united kingdom', 'inglaterra'], 'ie': ['ireland'],
    'nl': ['netherlands', 'holland', 'nederland'], 'ch': ['switzerland', 'schweiz', 'suica', 'suisse'],
    'at': ['austria', 'osterreich'], 'jp': ['japan', 'japao', 'nihon'], 'ph': ['philippines', 'filipinas'],
    'th': ['thailand'], 'au': ['australia'], 'nz': ['new zealand'], 'ca': ['canada'], 'is': ['iceland', 'islandia'],
    'no': ['norway', 'norge', 'noruega'], 'se': ['sweden', 'sverige', 'suecia'], 'gr': ['greece', 'grecia'],
    'tr': ['turkey', 'turkiye'], 'il': ['israel', 'jerusalem'], 'za': ['south africa'], 'cr': ['costa rica'],
    'do': ['dominican', 'punta cana'], 'cu': ['cuba'], 'id': ['indonesia', 'bali'], 'kr': ['korea'],
}


def norm(s):
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c)).lower()
    return ' ' + re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]', ' ', s)).strip() + ' '


def contem(texto_norm, termo):
    t = norm(termo).strip()
    return bool(t) and (' ' + t + ' ') in texto_norm


def pedacos(titulo):
    t = html.unescape(titulo)
    t = re.sub(r'#\w+', ' ', t)
    t = re.sub(r'[^\w\s,\-|/()\'.:&]', ' ', t)          # emojis e símbolos
    segs = re.split(r'\s[|\-–—]\s|\||:|\(|\)|\[|\]|!|\s/\s|--', t)
    out = []
    for s in segs:
        s = re.sub(r'\s+', ' ', LIXO.sub(' ', s)).strip(' ,.-/')
        if len(s) < 3 or s.isdigit():
            continue
        out.append(s)
        partes = [p.strip(' .') for p in s.split(',') if len(p.strip(' .')) >= 3]
        if len(partes) > 1:                             # "Sayulita, Mexico" -> também "Sayulita"
            out.append(', '.join(partes[:2]))
            out.extend(partes[:3])
    final = []
    for s in out:
        if norm(s).strip() not in GENERICO and s not in final:
            final.append(s)
    return final[:8]


def _photon(q):
    url = 'https://photon.komoot.io/api/?limit=3&q=' + urllib.parse.quote(q)
    req = urllib.request.Request(url, headers={'User-Agent': 'CameraWeb/1.0 (mapa pessoal)'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode('utf-8')).get('features', [])


def _aceita(p, titulo_n):
    tipo = p.get('osm_value') if p.get('osm_value') in TIPOS else p.get('type')
    if tipo not in TIPOS:
        return False
    nome = p.get('name', '')
    pal = [w for w in norm(nome).split() if len(w) > 2 and w not in ('de', 'do', 'da', 'dos', 'das', 'the', 'of')]
    if not pal or not all((' ' + w + ' ') in titulo_n for w in pal):
        return False                                    # o nome tem que estar no título
    if tipo in ('state', 'country'):
        return True
    cc = (p.get('countrycode') or '').lower()
    estado = p.get('state') or ''
    pistas = [estado, SIGLAS.get(norm(estado).strip(), ''), p.get('country') or ''] + PAISES.get(cc, [])
    return any(contem(titulo_n, x) for x in pistas if x and len(norm(x).strip()) >= 2)


def localizar(titulo, pausa=0.25):
    """Devolve (lat, lon, descrição) ou None."""
    titulo_n = norm(html.unescape(titulo))
    for q in pedacos(titulo):
        try:
            feats = _photon(q)
        except Exception:
            feats = []
        time.sleep(pausa)                               # gentileza com o servidor público do Photon
        for f in feats:
            p = f.get('properties', {})
            if _aceita(p, titulo_n):
                lon, lat = f['geometry']['coordinates']
                desc = ', '.join(x for x in (p.get('name'), p.get('state'), p.get('country')) if x)
                return lat, lon, desc
    return None


if __name__ == '__main__':   # teste: py geo_titulo.py resultado-da-busca.json
    import sys
    for it in json.load(open(sys.argv[1], encoding='utf-8'))['items']:
        t = it['snippet']['title']
        r = localizar(t)
        print(('%-60s -> %s' % (html.unescape(t)[:60], r and r[2])).encode('ascii', 'replace').decode())
