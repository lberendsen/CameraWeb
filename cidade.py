# -*- coding: utf-8 -*-
"""São Paulo agora: sensores públicos da cidade, reunidos pelo servidor.

  - Alagamentos do CGE da Prefeitura (página www.cgesp.org/v3/alagamentos.jsp),
    com cada rua localizada aproximadamente pelo Photon (OpenStreetMap).
  - Helicópteros no céu da cidade (ADS-B aberto, adsb.fi / adsb.lol), com detecção
    de "circulando": muitas voltas num raio pequeno costuma indicar algo no chão.
  - Ônibus da SPTrans (API Olho Vivo, precisa de token gratuito): velocidade de cada
    ônibus e os trechos onde muitos estão parados ao mesmo tempo.
  - Situação das linhas de metrô e trem (JSON público do Grupo CCR/Motiva, cobre todas).
  - Avisos meteorológicos do INMET para a capital (código IBGE 3550308).
  - Estado de alagamento de cada subprefeitura (cor do mapa do CGE, lida com Pillow).
  - Lentidão por zona e velocidade dos ônibus nos corredores (página inicial da CET).

Tudo só roda enquanto a página estiver pedindo (fica parado sem ninguém olhando).
"""
import html
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request

SP = (-23.60, -46.63)          # centro aproximado da cidade
UA = {'User-Agent': 'Mozilla/5.0 (CameraWeb, mapa pessoal)'}   # o INMET não responde a UAs que não parecem navegador
OCIOSO_SEG = 180
HELI_TIPOS = {'AS50', 'AS55', 'AS65', 'AS32', 'AS3B', 'EC20', 'EC30', 'EC35', 'EC45', 'EC55', 'EC75', 'H160', 'H175',
              'B06', 'B06T', 'B407', 'B412', 'B429', 'B430', 'B505', 'A109', 'A119', 'A139', 'A169', 'A189', 'AW09',
              'R22', 'R44', 'R66', 'S76', 'S92', 'BK17', 'MI8', 'UH1', 'H60', 'EH10', 'G2CA', 'A129', 'EXPL'}


def _json(url, timeout=12, dados=None, cab=None):
    req = urllib.request.Request(url, data=dados, headers=dict(UA, **(cab or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def distancia_m(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h))


class Cidade:
    def __init__(self, pasta, ler_token_sptrans):
        self.pasta = pasta
        self.ler_token = ler_token_sptrans
        self.trava = threading.Lock()
        self.ultimo_pedido = 0
        self.rodando = False
        self.arq_geo = os.path.join(pasta, 'cidade-geo.json')
        try:
            with open(self.arq_geo, encoding='utf-8') as f:
                self.geo = json.load(f)       # texto do endereço -> [lat, lon] ou None
        except (OSError, ValueError):
            self.geo = {}
        self.alagamentos, self.t_alag, self.erro_alag = [], 0, None
        self.helis = {}                       # hex -> {'pos': [(t, lat, lon)], 'a': dados}
        self.erro_heli = None
        self.onibus = {}                      # prefixo -> {'pos': [(t, lat, lon)], 'linha': ...}
        self.erro_onibus, self.t_onibus, self.cookie = None, 0, None
        self.linhas, self.t_linhas = [], 0
        self.avisos = []
        self.cge, self.cge_areas = None, None
        self.cet, self.cet_mudou = None, 0

    # ---------- chamado pela página ----------
    def consultar(self):
        with self.trava:
            self.ultimo_pedido = time.time()
            if not self.rodando:
                self.rodando = True
                threading.Thread(target=self._laco, daemon=True).start()
            agora = time.time()
            helis = []
            for hx, h in self.helis.items():
                if agora - h['pos'][-1][0] > 120:
                    continue
                t, lat, lon = h['pos'][-1]
                giro = self._circulando(h['pos'])
                a = h['a']
                helis.append({'hex': hx, 'lat': lat, 'lon': lon, 'nome': (a.get('flight') or '').strip() or a.get('r') or hx,
                              'modelo': a.get('desc') or a.get('t') or '', 'reg': a.get('r') or '',
                              'alt': a.get('alt_baro'), 'gs': a.get('gs'), 'rumo': a.get('track'),
                              'rastro': [[p[1], p[2]] for p in h['pos'][-40:]], 'circulando': giro})
            return {
                'alagamentos': self.alagamentos, 'alag_atualizado': self.t_alag, 'erro_alag': self.erro_alag,
                'helicopteros': helis, 'erro_heli': self.erro_heli,
                'onibus': self._resumo_onibus(), 'agora': agora,
                'linhas': self.linhas, 'linhas_atualizado': self.t_linhas,
                'avisos': [a for a in self.avisos if (a['fim'] or '') >= time.strftime('%Y-%m-%d %H:%M')],
                'cge': self.cge,
                'cet': dict(self.cet, parado_ha_min=round((agora - self.cet_mudou) / 60)) if self.cet else None,
            }

    # ---------- laço de coleta ----------
    def _laco(self):
        prox = {'alag': 0, 'heli': 0, 'onibus': 0, 'linhas': 0, 'inmet': 0, 'cge': 0, 'cet': 0}
        try:
            while time.time() - self.ultimo_pedido < OCIOSO_SEG:
                agora = time.time()
                if agora >= prox['alag']:
                    prox['alag'] = agora + 180
                    self._coletar_alagamentos()
                if agora >= prox['heli']:
                    prox['heli'] = agora + 12
                    self._coletar_helis()
                for nome, cada, func in (('linhas', 120, self._coletar_linhas), ('inmet', 600, self._coletar_inmet),
                                         ('cge', 300, self._coletar_cge), ('cet', 300, self._coletar_cet)):
                    if agora >= prox[nome]:
                        prox[nome] = agora + cada
                        try:
                            func()
                        except Exception:
                            pass          # uma fonte fora do ar não derruba as outras
                if agora >= prox['onibus'] and self.ler_token():
                    prox['onibus'] = agora + 30
                    self._coletar_onibus()
                time.sleep(1)
        finally:
            self.rodando = False

    # ---------- alagamentos (CGE) ----------
    def _coletar_alagamentos(self):
        hoje = time.strftime('%d/%m/%Y', time.localtime())
        url = 'https://www.cgesp.org/v3/alagamentos.jsp?dataBusca=' + urllib.parse.quote(hoje) + '&enviaBusca=Buscar'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (CameraWeb)'})
            with urllib.request.urlopen(req, timeout=20) as r:
                pagina = r.read().decode('iso-8859-1', 'replace')
        except Exception as e:
            self.erro_alag = str(e)
            return
        lista = []
        zona = bairro = ''
        # percorre a página em ordem: títulos de zona, nomes de bairro e cada ponto
        padrao = re.compile(r'<h1 class="tit-bairros">(.*?)</h1>|<td class="bairro[^"]*">\s*(.*?)\s*<hr|'
                            r'<li class="(ativo|inativo)-(transitavel|intransitavel)"[^>]*>.*?'
                            r'col-local">(.*?)<br/>(.*?)</li>.*?<li class="arial-descr-alag">(.*?)<br/>(.*?)</li>', re.S)
        for m in padrao.finditer(pagina):
            if m.group(1):
                zona = html.unescape(m.group(1)).strip()
            elif m.group(2):
                bairro = html.unescape(re.sub(r'<[^>]+>', '', m.group(2))).strip()
            else:
                rua = html.unescape(m.group(6)).strip()
                ref = html.unescape(re.sub(r'^\s*Refer\S*:\s*', '', m.group(8))).strip()
                pos = self._localizar(rua, ref, bairro)
                lista.append({'ativo': m.group(3) == 'ativo', 'transitavel': m.group(4) == 'transitavel',
                              'horario': html.unescape(m.group(5)).strip(), 'rua': rua, 'bairro': bairro, 'zona': zona,
                              'sentido': html.unescape(re.sub(r'^\s*Sentido:\s*', '', m.group(7))).strip(), 'ref': ref,
                              'lat': pos[0] if pos else None, 'lon': pos[1] if pos else None})
        with self.trava:
            self.alagamentos, self.t_alag, self.erro_alag = lista, time.time(), None

    def _localizar(self, rua, ref, bairro):
        # "AV VITAL BRAZIL" + bairro, pelo Photon; a posição é aproximada (meio da rua no bairro)
        ruas = {'AV ': 'Avenida ', 'R ': 'Rua ', 'AL ': 'Alameda ', 'PCA ': 'Praça ', 'PC ': 'Praça ', 'EST ': 'Estrada ',
                'VD ': 'Viaduto ', 'PTE ': 'Ponte ', 'TV ': 'Travessa ', 'MARGINAL ': 'Marginal '}
        nome = rua.upper()
        for k, v in ruas.items():
            if nome.startswith(k):
                nome = v + nome[len(k):]
                break
        chave = '%s | %s' % (nome.title(), bairro)
        if chave in self.geo:
            return self.geo[chave]
        achou = None
        for q in ('%s, %s, São Paulo' % (nome.title(), bairro), '%s, São Paulo' % nome.title()):
            try:
                f = _json('https://photon.komoot.io/api/?limit=1&lat=%s&lon=%s&q=%s' % (SP[0], SP[1], urllib.parse.quote(q)))
                feats = f.get('features', [])
                if feats and feats[0]['properties'].get('city') in ('São Paulo', 'Sao Paulo'):
                    lon, lat = feats[0]['geometry']['coordinates']
                    achou = [lat, lon]
                    break
            except Exception:
                pass
            time.sleep(0.3)
        self.geo[chave] = achou
        try:
            with open(self.arq_geo, 'w', encoding='utf-8') as f:
                json.dump(self.geo, f, ensure_ascii=False)
        except OSError:
            pass
        return achou

    # ---------- helicópteros ----------
    @staticmethod
    def eh_helicoptero(a):
        return a.get('category') == 'A7' or (a.get('t') or '').upper() in HELI_TIPOS or 'HELICOP' in (a.get('desc') or '').upper()

    def _coletar_helis(self):
        dados = None
        for url in ('https://opendata.adsb.fi/api/v2/lat/%s/lon/%s/dist/30' % SP,
                    'https://api.adsb.lol/v2/lat/%s/lon/%s/dist/30' % SP):
            try:
                dados = _json(url, timeout=8)
                break
            except Exception as e:
                self.erro_heli = str(e)
        if dados is None:
            return
        agora = time.time()
        with self.trava:
            for a in dados.get('ac') or dados.get('aircraft') or []:
                if a.get('lat') is None or not self.eh_helicoptero(a):
                    continue
                h = self.helis.setdefault(a['hex'], {'pos': []})
                h['a'] = a
                t = agora - (a.get('seen_pos') or 0)
                if not h['pos'] or t - h['pos'][-1][0] > 3:
                    h['pos'].append((t, a['lat'], a['lon']))
                h['pos'] = [p for p in h['pos'] if agora - p[0] < 900]
            for hx in [k for k, v in self.helis.items() if not v['pos'] or agora - v['pos'][-1][0] > 900]:
                del self.helis[hx]
            self.erro_heli = None

    @staticmethod
    def _circulando(pos):
        """Se nos últimos 8 min o helicóptero girou >= 1 volta num raio de até 3 km: centro e raio."""
        recentes = [p for p in pos if pos[-1][0] - p[0] < 480]
        if len(recentes) < 8:
            return None
        clat = sum(p[1] for p in recentes) / len(recentes)
        clon = sum(p[2] for p in recentes) / len(recentes)
        raio = max(distancia_m((clat, clon), (p[1], p[2])) for p in recentes)
        if raio > 3000:
            return None
        giro, rumo_ant = 0.0, None
        for a, b in zip(recentes, recentes[1:]):
            if distancia_m((a[1], a[2]), (b[1], b[2])) < 30:
                continue
            rumo = math.degrees(math.atan2(b[2] - a[2], b[1] - a[1]))
            if rumo_ant is not None:
                giro += ((rumo - rumo_ant + 540) % 360) - 180
            rumo_ant = rumo
        if abs(giro) < 330:
            return None
        return {'lat': clat, 'lon': clon, 'raio': round(raio), 'voltas': round(abs(giro) / 360, 1)}

    # ---------- metrô e trens ----------
    def _coletar_linhas(self):
        d = _json('https://webapi.grupoccr.com.br/v1/mobility/public/line-status/current/state/SP', timeout=15)
        d = d.get('data', d)
        lista = []
        for c in d.get('concessoes', []):
            for l in c.get('linhas', []):
                st = l.get('statusLinha') or {}
                lista.append({'numero': l.get('numero'), 'nome': l.get('nome'), 'cor': l.get('corRgb'), 'operadora': c.get('nome'),
                              'codigo': st.get('codigo'), 'status': st.get('status'), 'descricao': (st.get('descricao') or '').strip()})
        lista.sort(key=lambda l: int(re.sub(r'\D', '', str(l['numero'])) or 99))
        with self.trava:
            self.linhas, self.t_linhas = lista, time.time()

    # ---------- avisos do INMET ----------
    def _coletar_inmet(self):
        d = _json('https://apiprevmet3.inmet.gov.br/avisos/ativos', timeout=30)
        vistos, lista = set(), []
        for a in d.get('hoje', []) + d.get('futuro', []):
            if '3550308' not in str(a.get('geocodes', '')).split(',') or a.get('id') in vistos:
                continue
            vistos.add(a.get('id'))
            lista.append({'id': a.get('id'), 'evento': a.get('descricao'), 'severidade': a.get('severidade'),
                          'inicio': a.get('inicio'), 'fim': a.get('fim'), 'riscos': a.get('riscos'), 'instrucoes': a.get('instrucoes')})
        lista.sort(key=lambda a: a['inicio'] or '')
        with self.trava:
            self.avisos = lista

    # ---------- estado das subprefeituras (mapa do CGE) ----------
    # O CGE só publica isso como imagem: uma cor por subprefeitura. Lemos a cor em volta do
    # centro de cada uma (coordenadas tiradas do mapa clicável do próprio site).
    NIVEIS = [((255, 0, 0), 'Alerta', 3), ((255, 151, 0), 'Iminência de transbordamento', 2), ((255, 255, 0), 'Atenção', 1)]

    def _coletar_cge(self):
        try:
            from PIL import Image
        except ImportError:
            return
        import io as _io
        if self.cge_areas is None:
            req = urllib.request.Request('https://www.cgesp.org/v3/mapas.jsp?arq=alertas', headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                pag = r.read().decode('utf-8', 'replace')     # esta página do CGE é UTF-8 (a de alagamentos é Latin-1)
            self.cge_areas = [(int(x), int(y), html.unescape(t)) for x, y, t in
                              re.findall(r'<area[^>]*coords="(\d+),(\d+),\d+"[^>]*title="([^"]+)"', pag)]
        req = urllib.request.Request('https://www.cgesp.org/cgesp/mapa_sp_geoserver.png', headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            img = Image.open(_io.BytesIO(r.read())).convert('RGB')
        regioes, pior = [], (0, 'Observação')
        for x, y, nome in self.cge_areas:
            cont = {}
            for dx in range(-8, 9, 2):
                for dy in range(-8, 9, 2):
                    try:
                        c = img.getpixel((x + dx, y + dy))
                    except IndexError:
                        continue
                    for ref, rot, n in self.NIVEIS:
                        if sum(abs(a - b) for a, b in zip(c, ref)) < 60:
                            cont[n] = cont.get(n, 0) + 1
            nivel = max((n for n, k in cont.items() if k >= 6), default=0)
            if nivel:
                rot = [r for _, r, n in self.NIVEIS if n == nivel][0]
                pos = self._local_subprefeitura(nome)   # para o "Meu bairro" saber se é a região dele
                regioes.append({'nome': nome, 'nivel': nivel, 'estado': rot,
                                'lat': pos[0] if pos else None, 'lon': pos[1] if pos else None})
                if nivel > pior[0]:
                    pior = (nivel, rot)
        with self.trava:
            self.cge = {'estado': pior[1], 'nivel': pior[0], 'regioes': sorted(regioes, key=lambda r: -r['nivel']), 't': time.time()}

    def _local_subprefeitura(self, nome):
        chave = 'SUB | ' + nome
        if chave not in self.geo:
            achou = None
            for q in ('Subprefeitura %s, São Paulo' % nome, '%s, São Paulo' % nome.split('/')[0]):
                try:
                    f = _json('https://photon.komoot.io/api/?limit=1&lat=%s&lon=%s&q=%s' % (SP[0], SP[1], urllib.parse.quote(q)))
                    feats = f.get('features', [])
                    if feats and feats[0]['properties'].get('city') in ('São Paulo', 'Sao Paulo'):
                        lon, lat = feats[0]['geometry']['coordinates']
                        achou = [lat, lon]
                        break
                except Exception:
                    pass
            self.geo[chave] = achou
        return self.geo[chave]

    # ---------- lentidão e ônibus nos corredores (página inicial da CET) ----------
    def _coletar_cet(self):
        req = urllib.request.Request('https://www.cetsp.com.br/', headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            pag = r.read().decode('utf-8', 'replace')
        zonas = {z: int(km) for z, km in re.findall(r'<h3>(Norte|Oeste|Centro|Leste|Sul)</h3>\s*<h4>(\d+)\s*km</h4>', pag)}
        vel = re.findall(r'class="(CentroBairro|BairroCentro)">.*?<strong class="valor">(\d+)\s*km/h', pag, re.S)
        rod = re.search(r'Rod.zio:\s*([^<]*)<', pag)
        if not zonas:
            return
        novo = {'zonas': zonas, 'total': sum(zonas.values()), 'onibus_corredores': {k: int(v) for k, v in vel},
                'rodizio': (rod.group(1).strip() if rod else '') or '-'}
        with self.trava:
            if not self.cet or self.cet['zonas'] != zonas:
                self.cet_mudou = time.time()     # se nunca muda, a página da CET pode estar congelada
            self.cet = novo

    # ---------- ônibus (SPTrans Olho Vivo) ----------
    def _coletar_onibus(self):
        base = 'https://api.olhovivo.sptrans.com.br/v2.1'
        try:
            if not self.cookie:
                req = urllib.request.Request(base + '/Login/Autenticar?token=' + urllib.parse.quote(self.ler_token()),
                                             data=b'', headers=UA, method='POST')
                with urllib.request.urlopen(req, timeout=15) as r:
                    ok = r.read().decode().strip().lower() == 'true'
                    biscoito = r.headers.get('Set-Cookie', '')
                if not ok:
                    self.erro_onibus = 'token'
                    return
                self.cookie = biscoito.split(';')[0]
            d = _json(base + '/Posicao', timeout=25, cab={'Cookie': self.cookie})
        except urllib.error.HTTPError as e:
            self.cookie = None
            self.erro_onibus = 'http %s' % e.code
            return
        except Exception as e:
            self.erro_onibus = str(e)
            return
        agora = time.time()
        with self.trava:
            for linha in d.get('l', []):
                letreiro = '%s %s' % (linha.get('c', ''), linha.get('lt0') if linha.get('sl') == 2 else linha.get('lt1'))
                for v in linha.get('vs', []):
                    o = self.onibus.setdefault(v['p'], {'pos': []})
                    o['linha'] = letreiro.strip()
                    if v.get('ta') == o.get('ta'):
                        continue          # o GPS não mandou leitura nova: não dá para dizer se andou
                    o['ta'], o['visto'] = v.get('ta'), agora
                    atual = (v['py'], v['px'])
                    if not o['pos'] or (o['pos'][-1][1], o['pos'][-1][2]) != atual:
                        o['pos'].append((agora, atual[0], atual[1]))
                        o.pop('parado_desde', None)
                    else:                 # leitura nova, mesmo lugar: está parado
                        o.setdefault('parado_desde', o['pos'][-1][0])
                    o['pos'] = o['pos'][-6:]
            for p in [k for k, v in self.onibus.items() if agora - v.get('visto', 0) > 600]:
                del self.onibus[p]
            self.t_onibus, self.erro_onibus = agora, None

    def _resumo_onibus(self):
        if not self.ler_token():
            return {'erro': 'sem-token'}
        if self.erro_onibus and not self.onibus:
            return {'erro': self.erro_onibus}
        agora = time.time()
        velocidades, parados, pontos = [], [], []
        for p, o in self.onibus.items():
            pos = o['pos']
            if agora - o.get('visto', 0) > 180 or not pos:
                continue
            vel = None
            if len(pos) >= 2 and pos[-1][0] - pos[-2][0] > 20:
                vel = distancia_m(pos[-2][1:], pos[-1][1:]) / (pos[-1][0] - pos[-2][0]) * 3.6
            elif o.get('parado_desde'):
                vel = 0.0
            if vel is not None and vel < 120:
                velocidades.append(vel)
            preso = o.get('parado_desde') and agora - o['parado_desde'] > 240
            if preso:
                parados.append((pos[-1][1], pos[-1][2], o['linha']))
            pontos.append([round(pos[-1][1], 5), round(pos[-1][2], 5), None if vel is None else round(vel), o['linha']])
        # trechos com 4+ ônibus de linhas diferentes parados há 4+ min a menos de 300 m uns dos outros
        grupos = []
        usados = set()
        for i, a in enumerate(parados):
            if i in usados:
                continue
            perto = [j for j, b in enumerate(parados) if j not in usados and distancia_m(a[:2], b[:2]) < 300]
            linhas = {parados[j][2] for j in perto}
            if len(linhas) >= 4:
                usados.update(perto)
                grupos.append({'lat': sum(parados[j][0] for j in perto) / len(perto),
                               'lon': sum(parados[j][1] for j in perto) / len(perto),
                               'onibus': len(perto), 'linhas': sorted(linhas)[:8]})
        return {'total': len(pontos), 'vel_media': round(sum(velocidades) / len(velocidades), 1) if velocidades else None,
                'parados': len(parados), 'trechos_parados': grupos, 'pontos': pontos,
                'atualizado': self.t_onibus, 'erro': self.erro_onibus}
