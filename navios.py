# -*- coding: utf-8 -*-
"""Navios em tempo real (AIS) pela AISStream.io.

A AISStream só aceita conexões vindas de um servidor (não do navegador), por um
WebSocket contínuo. Este módulo mantém UMA conexão aberta enquanto a página estiver
pedindo navios, assina só a área que está na tela e guarda a última posição de cada
navio. Usa só a biblioteca padrão do Python (WebSocket escrito à mão, com a compressão
permessage-deflate que a AISStream pede).
"""
import base64
import json
import os
import socket
import ssl
import struct
import threading
import time
import zlib

URL_HOST = 'stream.aisstream.io'
URL_PATH = '/v0/stream'
TIPOS_MSG = ['PositionReport', 'StandardClassBPositionReport', 'ExtendedClassBPositionReport', 'ShipStaticData']
VALIDADE_SEG = 20 * 60     # navio sem notícia há 20 min some do mapa
OCIOSO_SEG = 120           # ninguém pediu navios há 2 min: fecha a conexão


# ---------- WebSocket mínimo (cliente) ----------

class WebSocket:
    def __init__(self, host, path):
        raw = socket.create_connection((host, 443), timeout=20)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        chave = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            'GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
            'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n'
            'Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits\r\n'
            'User-Agent: CameraWeb/1.0\r\n\r\n' % (path, host, chave)).encode())
        self.buf = b''
        while b'\r\n\r\n' not in self.buf:
            pedaco = self.sock.recv(4096)
            if not pedaco:
                raise ConnectionError('conexão fechada no aperto de mão')
            self.buf += pedaco
        cab, self.buf = self.buf.split(b'\r\n\r\n', 1)
        linhas = cab.decode('latin-1').split('\r\n')
        if ' 101 ' not in linhas[0] + ' ':
            raise ConnectionError('resposta inesperada: ' + linhas[0])
        ext = ' '.join(l.split(':', 1)[1] for l in linhas[1:] if l.lower().startswith('sec-websocket-extensions:'))
        self.deflate = 'permessage-deflate' in ext
        self.sem_contexto = 'server_no_context_takeover' in ext
        self.inflador = zlib.decompressobj(-15)
        self.sock.settimeout(None)
        self.trava_envio = threading.Lock()

    def _ler(self, n):
        while len(self.buf) < n:
            pedaco = self.sock.recv(65536)
            if not pedaco:
                raise ConnectionError('conexão fechada')
            self.buf += pedaco
        dados, self.buf = self.buf[:n], self.buf[n:]
        return dados

    def _enviar_quadro(self, opcode, dados):
        cab = bytes([0x80 | opcode])
        n = len(dados)
        if n < 126:
            cab += bytes([0x80 | n])
        elif n < 65536:
            cab += bytes([0x80 | 126]) + struct.pack('>H', n)
        else:
            cab += bytes([0x80 | 127]) + struct.pack('>Q', n)
        mascara = os.urandom(4)
        corpo = bytes(b ^ mascara[i % 4] for i, b in enumerate(dados))
        with self.trava_envio:
            self.sock.sendall(cab + mascara + corpo)

    def enviar(self, texto):
        self._enviar_quadro(0x1, texto.encode('utf-8'))

    def receber(self):
        """Devolve a próxima mensagem (bytes) ou None se o servidor fechou."""
        partes, comprimida = [], False
        while True:
            b1, b2 = self._ler(2)
            fin, rsv1, opcode = b1 & 0x80, b1 & 0x40, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack('>H', self._ler(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._ler(8))[0]
            mascara = self._ler(4) if b2 & 0x80 else None
            dados = self._ler(n)
            if mascara:
                dados = bytes(b ^ mascara[i % 4] for i, b in enumerate(dados))
            if opcode == 0x8:      # close
                return None
            if opcode == 0x9:      # ping -> pong
                self._enviar_quadro(0xA, dados)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                comprimida = bool(rsv1)
            partes.append(dados)
            if fin:
                msg = b''.join(partes)
                if comprimida and self.deflate:
                    if self.sem_contexto:
                        self.inflador = zlib.decompressobj(-15)
                    msg = self.inflador.decompress(msg + b'\x00\x00\xff\xff')
                return msg

    def fechar(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------- estado dos navios ----------

class Navios:
    def __init__(self, ler_chave):
        self.ler_chave = ler_chave
        self.trava = threading.Lock()
        self.navios = {}          # mmsi -> dados
        self.caixa = None         # lista de áreas [[lat_s, lon_w], [lat_n, lon_e]] assinadas agora
        self.areas = {}           # área pedida -> quando foi pedida (várias abas podem olhar lugares diferentes)
        self.ultimo_pedido = 0
        self.ws = None
        self.rodando = False
        self.erro = None
        self.estado = 'parado'
        self.compressao = None
        self.confirmado = False

    # chamado a cada consulta da página
    def consultar(self, s, w, n, e):
        if not self.ler_chave():
            return {'erro': 'sem-chave'}
        with self.trava:
            self.ultimo_pedido = time.time()
            # assina uma área um pouco maior que a tela, para não reassinar a cada arrasto
            ms, mw = (n - s) * 0.25, (e - w) * 0.25
            area = ((round(max(-90, s - ms), 2), round(max(-180, w - mw), 2)),
                    (round(min(90, n + ms), 2), round(min(180, e + mw), 2)))
            self.areas[area] = time.time()
            if self.erro == 'chave':
                # chave recusada: não insiste até chegar uma chave nova (chave_nova)
                return {'erro': 'chave'}
            if not self.rodando:
                self.rodando = True
                self.erro = None
                threading.Thread(target=self._laco, daemon=True).start()
            agora = time.time()
            for k in [k for k, v in self.navios.items() if agora - v['t'] > VALIDADE_SEG]:
                del self.navios[k]
            lista = [dict(v, mmsi=k) for k, v in self.navios.items()
                     if 'lat' in v and s <= v['lat'] <= n and w <= v['lon'] <= e]
            lista.sort(key=lambda v: -v['t'])
            return {'navios': lista[:1500], 'estado': self.estado, 'erro': self.erro,
                    'compressao': self.compressao, 'agora': agora}

    @property
    def caixa_pedida(self):
        # áreas pedidas nos últimos 30 s (no máx. 6, as mais recentes); chamar com a trava
        agora = time.time()
        for a in [a for a, t in self.areas.items() if agora - t > 30]:
            del self.areas[a]
        recentes = sorted(self.areas, key=lambda a: -self.areas[a])[:6]
        return [[list(a[0]), list(a[1])] for a in sorted(recentes)] or None

    def chave_nova(self):
        with self.trava:
            self.erro = None
        if self.ws:
            self.ws.fechar()   # reconecta com a chave nova

    def _assinar(self, ws, caixa):
        ws.enviar(json.dumps({'APIKey': self.ler_chave(), 'BoundingBoxes': caixa,
                                   'FilterMessageTypes': TIPOS_MSG}))
        self.caixa = caixa

    def _vigia(self, ws):
        # reassina quando a tela muda (no máx. 1x/s, regra da AISStream) e fecha se ocioso
        while self.ws is ws:
            time.sleep(1.2)
            with self.trava:
                ocioso = time.time() - self.ultimo_pedido > OCIOSO_SEG
                pedida = self.caixa_pedida
            if ocioso:
                ws.fechar()
                return
            if pedida and pedida != self.caixa:
                try:
                    self._assinar(ws, pedida)
                except (OSError, AttributeError):
                    return

    def _laco(self):
        espera = 2
        try:
            while True:
                with self.trava:
                    if time.time() - self.ultimo_pedido > OCIOSO_SEG:
                        break
                    caixa = self.caixa_pedida
                self.estado = 'conectando'
                self.confirmado = False
                try:
                    self.ws = WebSocket(URL_HOST, URL_PATH)
                    self._assinar(self.ws, caixa)
                    threading.Thread(target=self._vigia, args=(self.ws,), daemon=True).start()
                    self.estado = 'recebendo'
                    espera = 2
                    while True:
                        msg = self.ws.receber()
                        if msg is None:
                            break
                        self._tratar(msg)
                except Exception as e:  # cai, espera um pouco e reconecta
                    if not self.erro:
                        self.erro = 'rede: %s' % e
                finally:
                    # a AISStream fecha a conexão sem explicar quando a chave é recusada
                    if self.ws and not self.confirmado and (self.erro or '').startswith('rede'):
                        self.erro = 'chave'
                    if self.ws:
                        self.ws.fechar()
                    self.ws = None
                    self.caixa = None
                if self.erro == 'chave':
                    break
                time.sleep(espera)
                espera = min(espera * 2, 60)
        finally:
            self.estado = 'parado'
            self.rodando = False

    def _tratar(self, bruto):
        try:
            d = json.loads(bruto.decode('utf-8'))
        except ValueError:
            return
        if 'error' in d:   # ex.: {"error": "Api Key Is Not Valid"}
            self.erro = 'chave' if 'key' in str(d['error']).lower() else str(d['error'])
            self.ws.fechar()
            return
        tipo = d.get('MessageType')
        if tipo == 'SubscriptionConfirmation':
            self.compressao = (d.get('Message') or {}).get('CompressionEnabled')
            self.confirmado = True
            self.erro = None
            return
        meta = d.get('MetaData') or {}
        mmsi = meta.get('MMSI')
        if not mmsi:
            return
        corpo = (d.get('Message') or {}).get(tipo) or {}
        with self.trava:
            v = self.navios.setdefault(str(mmsi), {'t': 0})
            nome = (meta.get('ShipName') or '').strip()
            if nome:
                v['nome'] = nome
            if tipo == 'ShipStaticData':
                v['tipo'] = corpo.get('Type')
                dest = (corpo.get('Destination') or '').strip()
                if dest:
                    v['destino'] = dest
                if (corpo.get('Name') or '').strip():
                    v['nome'] = corpo['Name'].strip()
                dim = corpo.get('Dimension') or {}
                if dim.get('A') or dim.get('B'):
                    v['comp'] = (dim.get('A') or 0) + (dim.get('B') or 0)   # proa + popa, em metros
                return
            lat = meta.get('latitude', meta.get('Latitude', corpo.get('Latitude')))
            lon = meta.get('longitude', meta.get('Longitude', corpo.get('Longitude')))
            if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
                return
            v.update(lat=lat, lon=lon, t=time.time(),
                     sog=corpo.get('Sog'), cog=corpo.get('Cog'), rumo=corpo.get('TrueHeading'))
