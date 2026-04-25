"""
client/session.py
─────────────────────────────────────────────────────────────────────────────
Gerencia a sessão do cliente:
  • Descoberta de broker via Registry (REQ/REP)
  • Conexão aos sockets do broker (PUB + SUB + CONTROL)
  • Join / Leave de sala
  • Heartbeat periódico para o broker
  • Failover automático se o broker atual cair
"""

import os
import sys
import time
import threading
import logging
import msgpack as _mp
import zmq as _zmq

import zmq

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.protocol import (
    encode, decode,
    encode_with_topic,
    MSG_TEXT, MSG_AUDIO, MSG_VIDEO, MSG_CONTROL,
    CTRL_JOIN, CTRL_LEAVE,
)
from common.channels import (
    REGISTRY_ADDR,
    HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT,
    HWM,
)

log = logging.getLogger("client.session")


class SessionError(Exception):
    pass


class Session:
    def __init__(self, client_id: str, strategy: str = "round_robin"):
        self.client_id   = client_id
        self.strategy    = strategy
        self.ctx         = zmq.Context.instance()

        # Estado da sessão
        self.broker_info: dict | None = None
        self.current_room: str        = ""
        self._connected               = False
        self._running                 = False

        # Sockets
        self._pub_sock:     zmq.Socket | None = None
        self._sub_sock:     zmq.Socket | None = None
        self._control_sock: zmq.Socket | None = None

        # Callbacks registrados por tópico
        self._callbacks: dict[str, callable] = {}

        # Rastreamento de sequência de áudio por remetente (QoS)
        self._audio_last_seq: dict[str, int] = {}
        
        # Locks de proteção contra condições de corrida no failover
        self._lock     = threading.RLock()  # Protege estado da conexão e sockets
        self._cb_lock  = threading.Lock()   # Protege dicionário de callbacks
        self._ctrl_lock = threading.Lock()  # Serializa acesso ao control_sock

        # Contador de heartbeats perdidos (detecta falha de broker)
        self._hb_missed = 0

        # Contadores de QoS
        self._audio_seq  = 0    # número de sequência para áudio (detecta perdas)
        self._vid_drops  = 0    # frames de vídeo descartados consecutivos

        # Threads
        self._recv_thread: threading.Thread | None = None
        self._hb_thread:   threading.Thread | None = None

    # ══════════════════════════════════════════════════════════════════════════
    # Ciclo de vida
    # ══════════════════════════════════════════════════════════════════════════

    def connect(self) -> None:
        """Descobre um broker no registry e conecta todos os sockets iniciais."""
        self.broker_info = self._discover_broker()
        self._open_sockets()
        
        self._running  = True
        self._connected = True

        # Inicia threads apenas UMA VEZ
        self._recv_thread = threading.Thread(
            target=self._thread_receive, daemon=True, name=f"recv-{self.client_id}"
        )
        self._hb_thread = threading.Thread(
            target=self._thread_heartbeat, daemon=True, name=f"hb-{self.client_id}"
        )
        self._recv_thread.start()
        self._hb_thread.start()

        log.info(
            "Conectado ao broker %s (%s)",
            self.broker_info["broker_id"],
            self.broker_info["host"],
        )

    def disconnect(self) -> None:
        """Sai da sala atual e fecha todos os sockets."""
        self._running   = False
        self._connected = False
        if self.current_room:
            try:
                self._send_control(CTRL_LEAVE, self.current_room)
            except Exception:
                pass
        self._close_sockets()
        log.info("Desconectado")

    def reconnect(self) -> None:
        """
        Failover: descobre novo broker, reconecta sockets, restaura sala.
        Também re-registra no Registry para que /who mostre o cliente no novo broker.
        """
        log.warning("Iniciando failover para outro broker…")

        with self._lock:
            self._connected = False
            room = self.current_room

            with self._cb_lock:
                cbs = dict(self._callbacks)

            self._close_sockets()
            time.sleep(1)

            try:
                self.broker_info = self._discover_broker()
                self._open_sockets()
                self._connected = True

                # Re-entra na sala no novo broker + atualiza Registry
                if room:
                    self._send_control(CTRL_JOIN, room)
                    # Notifica o Registry sobre a mudança de broker
                    self._registry_req({
                        "action":    "join",
                        "client_id": self.client_id,
                        "room":      room,
                        "broker_id": self.broker_info["broker_id"],
                    })

                # Restaura subscriptions
                for topic_str in cbs:
                    if self._sub_sock:
                        self._sub_sock.setsockopt(zmq.SUBSCRIBE, topic_str.encode())

                log.info("Failover concluído → broker %s",
                         self.broker_info["broker_id"])
            except Exception as e:
                log.error("Erro fatal durante failover: %s", e)

    # ══════════════════════════════════════════════════════════════════════════
    # API pública
    # ══════════════════════════════════════════════════════════════════════════

    def join(self, room: str) -> bool:
        with self._lock:
            if self.current_room and self.current_room != room:
                self.leave(self.current_room)

            ok = self._send_control(CTRL_JOIN, room)
            if ok:
                self.current_room = room
                log.info("Entrou na sala %s", room)
            return ok

    def leave(self, room: str) -> None:
        with self._lock:
            self._send_control(CTRL_LEAVE, room)
            if self.current_room == room:
                self.current_room = ""
            log.info("Saiu da sala %s", room)

    def subscribe(self, room: str, msg_type: str, callback: callable) -> None:
        topic_str = f"{room}.{msg_type}"
        with self._cb_lock:
            self._callbacks[topic_str] = callback
            
        with self._lock:
            if self._sub_sock:
                self._sub_sock.setsockopt(zmq.SUBSCRIBE, topic_str.encode())
        log.debug("Subscrito em %s", topic_str)

    def unsubscribe(self, room: str, msg_type: str) -> None:
        topic_str = f"{room}.{msg_type}"
        with self._cb_lock:
            self._callbacks.pop(topic_str, None)
            
        with self._lock:
            if self._sub_sock:
                self._sub_sock.setsockopt(zmq.UNSUBSCRIBE, topic_str.encode())

    def publish(self, msg_type: str, payload, room: str = "") -> bool:
        """
        Publica com QoS diferenciado por tipo:
          MSG_TEXT  → blocking com retry (garantia de entrega)
          MSG_AUDIO → NOBLOCK + seqno (detecta perdas no receptor)
          MSG_VIDEO → NOBLOCK (descarta se fila cheia)
        Retorna True se enviado, False se descartado.
        """
        with self._lock:
            if not self._connected or not self._pub_sock:
                raise SessionError("Não conectado")

            target_room = room or self.current_room
            if not target_room:
                raise SessionError("Nenhuma sala ativa")

            if msg_type == MSG_TEXT:
                return self._publish_text(target_room, payload)
            elif msg_type == MSG_AUDIO:
                return self._publish_audio(target_room, payload)
            else:
                return self._publish_media(msg_type, target_room, payload)

    def _publish_text(self, room: str, payload) -> bool:
        """Texto: tenta até 3 vezes com backoff curto (garantia de entrega)."""
        frames = encode_with_topic(MSG_TEXT, self.client_id, room, payload)
        for attempt in range(3):
            try:
                self._pub_sock.send_multipart(frames)
                return True
            except zmq.ZMQError:
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
        log.warning("MSG_TEXT descartado após 3 tentativas")
        return False

    def _publish_audio(self, room: str, payload) -> bool:
        """Áudio: envia com seqno para detecção de perda no receptor."""
        self._audio_seq += 1
        frames = encode_with_topic(
            MSG_AUDIO, self.client_id, room, payload,
            extra={"seq": self._audio_seq},
        )
        try:
            self._pub_sock.send_multipart(frames, zmq.NOBLOCK)
            return True
        except zmq.ZMQError:
            return False

    def _publish_media(self, msg_type: str, room: str, payload) -> bool:
        """Vídeo e outros: drop silencioso se fila cheia."""
        frames = encode_with_topic(msg_type, self.client_id, room, payload)
        try:
            self._pub_sock.send_multipart(frames, zmq.NOBLOCK)
            return True
        except zmq.ZMQError:
            return False

    def list_rooms(self) -> dict:
        """Consulta o Registry diretamente — nunca usa o control_sock."""
        return self._registry_req({"action": "list_rooms"})

    def who(self, room: str = "") -> list:
        """Consulta o Registry diretamente — nunca usa o control_sock."""
        r = room or self.current_room or "__"
        resp = self._registry_req({"action": "who", "room": r})
        return resp.get("members", [])

    def _registry_req(self, payload: dict) -> dict:
        """Faz um request ao Registry com socket temporário."""
        sock = self.ctx.socket(_zmq.REQ)
        sock.setsockopt(_zmq.RCVTIMEO, 3000)
        sock.setsockopt(_zmq.LINGER, 0)
        sock.connect(REGISTRY_ADDR)
        try:
            sock.send(encode(MSG_CONTROL, self.client_id, "__registry__", payload))
            resp = decode(sock.recv())
            return resp.get("data", {})
        except Exception as e:
            log.warning("registry_req falhou: %s", e)
            return {}
        finally:
            sock.close()

    # ══════════════════════════════════════════════════════════════════════════
    # Internos — sockets
    # ══════════════════════════════════════════════════════════════════════════

    def _open_sockets(self) -> None:
        ports = self.broker_info["ports"]
        host  = self.broker_info["host"]

        self._pub_sock = self.ctx.socket(zmq.PUB)
        self._pub_sock.setsockopt(zmq.SNDHWM, max(HWM.values()))
        self._pub_sock.connect(f"tcp://{host}:{ports['frontend']}")

        self._sub_sock = self.ctx.socket(zmq.SUB)
        self._sub_sock.setsockopt(zmq.RCVHWM, max(HWM.values()))
        self._sub_sock.connect(f"tcp://{host}:{ports['backend']}")

        self._control_sock = self.ctx.socket(zmq.DEALER)
        self._control_sock.setsockopt(zmq.IDENTITY, self.client_id.encode())
        self._control_sock.setsockopt(zmq.RCVTIMEO, 3000)
        self._control_sock.connect(f"tcp://{host}:{ports['control']}")

        time.sleep(0.1)

    def _close_sockets(self) -> None:
        for sock in (self._pub_sock, self._sub_sock, self._control_sock):
            if sock:
                try:
                    sock.close(linger=0)
                except Exception:
                    pass
        self._pub_sock     = None
        self._sub_sock     = None
        self._control_sock = None

    # ══════════════════════════════════════════════════════════════════════════
    # Internos — controle
    # ══════════════════════════════════════════════════════════════════════════

    def _send_control(self, action: str, room: str) -> bool:
        """Envia comando de controle ao broker. Serializado por _ctrl_lock."""
        with self._ctrl_lock:
            sock = self._control_sock
            if not sock:
                return False
            payload = {"action": action}
            raw = encode(MSG_CONTROL, self.client_id, room, payload)
            try:
                sock.send_multipart([b"", raw])
                frames = sock.recv_multipart()
                resp   = decode(frames[-1])
                return resp.get("data", {}).get("status") in ("joined", "left", "hb_ok", "ok")
            except zmq.ZMQError as e:
                log.warning("Controle falhou (%s): %s", action, e)
                return False

    def _send_control_and_wait(self, action: str, room: str) -> dict:
        """Envio síncrono ao broker. Serializado por _ctrl_lock."""
        with self._ctrl_lock:
            sock = self._control_sock
            if not sock:
                return {}
            payload = {"action": action}
            raw = encode(MSG_CONTROL, self.client_id, room, payload)
            try:
                sock.send_multipart([b"", raw])
                frames = sock.recv_multipart()
                return decode(frames[-1]).get("data", {})
            except zmq.ZMQError:
                return {}

    # ══════════════════════════════════════════════════════════════════════════
    # Internos — threads
    # ══════════════════════════════════════════════════════════════════════════

    def _thread_receive(self) -> None:
        """
        Loop de recepção de mensagens do SUB socket.
        Poll curto (300ms) — timeout aqui é NORMAL em salas ociosas,
        NÃO indica falha de broker. Quem detecta falha é _thread_heartbeat.
        """
        while self._running:
            with self._lock:
                sock      = self._sub_sock
                connected = self._connected

            if not connected or sock is None:
                time.sleep(0.3)
                continue

            try:
                if sock.poll(timeout=300) == 0:
                    continue  # sem mensagem — normal, continua

                frames = sock.recv_multipart(zmq.NOBLOCK)
                if len(frames) < 2:
                    continue

                topic_b  = frames[0]
                topic_str = topic_b.decode(errors="replace")

                # evita ExtraData com múltiplos pacotes
                unpacker = _mp.Unpacker(raw=False)
                unpacker.feed(frames[1])
                for msg in unpacker:
                    if msg.get("from") == self.client_id:
                        continue   # suprime eco da própria mensagem
                    # QoS áudio: detecta perda de pacotes por seqno
                    if topic_str.endswith(f".{MSG_AUDIO}"):
                        sender = msg.get("from", "")
                        seq    = msg.get("seq")
                        if seq is not None and sender in self._audio_last_seq:
                            gap = seq - self._audio_last_seq[sender] - 1
                            if gap > 0:
                                log.debug("Áudio: %d pacotes perdidos de %s", gap, sender)
                        if seq is not None:
                            self._audio_last_seq[sender] = seq
                    with self._cb_lock:
                        cb = self._callbacks.get(topic_str)
                    if cb:
                        try:
                            cb(msg)
                        except Exception as e:
                            log.error("Erro no callback de %s: %s", topic_str, e)

            except (zmq.ZMQError, AttributeError):
                pass

    def _thread_heartbeat(self) -> None:
        """
        Envia heartbeat periódico ao broker.
        Conta ACKs perdidos — após MAX_MISSED falhas consecutivas, faz failover.
        Este é o ÚNICO mecanismo de detecção de falha de broker.
        """
        MAX_MISSED = int(os.environ.get("HEARTBEAT_RETRIES", "3"))

        while self._running:
            time.sleep(HEARTBEAT_INTERVAL)

            with self._lock:
                connected = self._connected
                room      = self.current_room
                sock      = self._control_sock

            if not connected or sock is None:
                continue

            # Fora de sala, apenas reseta contador
            if not room:
                self._hb_missed = 0
                continue

            ack_ok = False
            with self._ctrl_lock:
                if sock:
                    try:
                        payload = {"action": "heartbeat"}
                        raw = encode(MSG_CONTROL, self.client_id, room, payload)
                        sock.send_multipart([b"", raw])
                        # Poll curto (500ms) para não segurar o lock por 3s inteiros
                        if sock.poll(timeout=500):
                            sock.recv_multipart()
                            ack_ok = True
                    except zmq.ZMQError:
                        pass

            if ack_ok:
                self._hb_missed = 0
            else:
                self._hb_missed += 1
                log.debug("Heartbeat sem ACK (%d/%d)", self._hb_missed, MAX_MISSED)
                if self._hb_missed >= MAX_MISSED:
                    log.warning("Broker sem resposta após %d heartbeats — failover",
                                MAX_MISSED)
                    self._hb_missed = 0
                    self.reconnect()

    # ══════════════════════════════════════════════════════════════════════════
    # Internos — descoberta
    # ══════════════════════════════════════════════════════════════════════════

    def _discover_broker(self) -> dict:
        retries = int(os.environ.get("HEARTBEAT_RETRIES", "5"))

        for attempt in range(1, retries + 1):
            sock = self.ctx.socket(zmq.REQ)
            sock.setsockopt(zmq.RCVTIMEO, 3000)
            sock.setsockopt(zmq.LINGER, 0)
            sock.connect(REGISTRY_ADDR)

            payload = {"action": "get_broker", "strategy": self.strategy}
            raw     = encode(MSG_CONTROL, self.client_id, "__registry__", payload)

            try:
                sock.send(raw)
                resp = decode(sock.recv())
                data = resp.get("data", {})
            except zmq.ZMQError as e:
                log.warning("Tentativa %d/%d de descoberta falhou: %s",
                            attempt, retries, e)
                sock.close()
                time.sleep(attempt * 0.5)
                continue
            finally:
                sock.close()

            if data.get("status") != "ok":
                log.warning("Registry respondeu: %s", data)
                time.sleep(0.5)
                continue

            # Verifica se o broker realmente está respondendo
            if self._probe_broker(data):
                log.info("Broker descoberto: %s @ %s",
                         data["broker_id"], data["host"])
                return data
            else:
                log.warning("Broker %s não respondeu ao probe — tentando outro",
                            data["broker_id"])
                time.sleep(1.0)

        raise SessionError(
            f"Não foi possível descobrir um broker ativo após {retries} tentativas"
        )

    def _probe_broker(self, broker_info: dict) -> bool:
        """Testa se o broker está respondendo enviando um ping de controle."""
        host  = broker_info.get("host", "")
        ports = broker_info.get("ports", {})
        if not host or not ports:
            return False
        sock = self.ctx.socket(zmq.DEALER)
        sock.setsockopt(zmq.RCVTIMEO, 1500)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.IDENTITY, f"{self.client_id}-probe".encode())
        sock.connect(f"tcp://{host}:{ports['control']}")
        try:
            raw = encode(MSG_CONTROL, self.client_id, "__probe__",
                         {"action": "heartbeat"})
            sock.send_multipart([b"", raw])
            sock.recv_multipart()
            return True
        except zmq.ZMQError:
            return False
        finally:
            sock.close()