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

import zmq

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.protocol import (
    encode, decode,
    encode_with_topic,
    MSG_TEXT, MSG_CONTROL,
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
        
        # Locks de proteção contra condições de corrida no failover
        self._lock = threading.RLock() # Protege estado da conexão e sockets
        self._cb_lock = threading.Lock() # Protege dicionário de callbacks

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
        """Failover: Reconecta sockets a um novo broker sem vazar threads."""
        log.warning("Iniciando failover para outro broker…")
        
        with self._lock:
            self._connected = False
            room = self.current_room
            
            with self._cb_lock:
                cbs = dict(self._callbacks)
            
            self._close_sockets()
            time.sleep(1) # Tempo para os sockets antigos morrerem bem
            
            try:
                self.broker_info = self._discover_broker()
                self._open_sockets()
                self._connected = True
                
                # Restaura estado
                if room:
                    self._send_control(CTRL_JOIN, room)
                
                # Restaura subscriptions
                for topic_str in cbs:
                    if self._sub_sock:
                        self._sub_sock.setsockopt(zmq.SUBSCRIBE, topic_str.encode())
                        
                log.info("Failover concluído com sucesso!")
            except Exception as e:
                log.error("Erro fatal durante failover: %s", e)
                # Mantém _connected = False para que as threads tentem de novo no próximo ciclo

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

    def publish(self, msg_type: str, payload, room: str = "") -> None:
        with self._lock:
            if not self._connected or not self._pub_sock:
                raise SessionError("Não conectado")
                
            target_room = room or self.current_room
            if not target_room:
                raise SessionError("Nenhuma sala ativa")

            frames = encode_with_topic(msg_type, self.client_id, target_room, payload)
            try:
                self._pub_sock.send_multipart(frames, zmq.NOBLOCK)
            except zmq.ZMQError:
                log.debug("publish descartado (fila cheia) — QoS %s", msg_type)

    def list_rooms(self) -> dict:
        return self._send_control_and_wait("list_rooms", self.current_room or "__")

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
        if not self._control_sock:
            return False
            
        payload = {"action": action}
        raw = encode(MSG_CONTROL, self.client_id, room, payload)
        try:
            self._control_sock.send_multipart([b"", raw])
            frames = self._control_sock.recv_multipart()
            resp   = decode(frames[-1])
            return resp.get("data", {}).get("status") in ("joined", "left", "hb_ok", "ok")
        except zmq.ZMQError as e:
            log.warning("Controle falhou (%s): %s", action, e)
            return False

    def _send_control_and_wait(self, action: str, room: str) -> dict:
        with self._lock:
            if not self._control_sock:
                return {}
            payload = {"action": action}
            raw = encode(MSG_CONTROL, self.client_id, room, payload)
            try:
                self._control_sock.send_multipart([b"", raw])
                frames = self._control_sock.recv_multipart()
                return decode(frames[-1]).get("data", {})
            except zmq.ZMQError:
                return {}

    # ══════════════════════════════════════════════════════════════════════════
    # Internos — threads
    # ══════════════════════════════════════════════════════════════════════════

    def _thread_receive(self) -> None:
        """Loop de recepção com poll seguro em relação a failovers."""
        missed = 0

        while self._running:
            with self._lock:
                sock = self._sub_sock
                connected = self._connected

            if not connected or sock is None:
                time.sleep(0.5)
                continue

            try:
                event = sock.poll(timeout=int(HEARTBEAT_TIMEOUT * 1000))

                if event == 0:
                    # Timeout
                    missed += 1
                    if missed >= 3:
                        log.warning("Broker sem resposta — tentando failover")

                        self.reconnect()
                        missed = 0
                    continue

                missed = 0

                # Lê a mensagem
                frames = sock.recv_multipart(zmq.NOBLOCK)
                if len(frames) < 2:
                    continue
                    
                topic_b, raw = frames[0], frames[1]
                topic_str    = topic_b.decode(errors="replace")
                msg = decode(raw)

                with self._cb_lock:
                    cb = self._callbacks.get(topic_str)

                if cb:
                    try:
                        cb(msg)
                    except Exception as e:
                        log.error("Erro no callback de %s: %s", topic_str, e)

            except (zmq.ZMQError, AttributeError):
                # Se o socket fechar no meio da operação, apenas ignora este ciclo
                pass

    def _thread_heartbeat(self) -> None:
        """Envia heartbeat periódico com segurança atômica."""
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL)
            
            with self._lock:
                sock = self._control_sock
                connected = self._connected
                room = self.current_room
            
            if not connected or sock is None or not room:
                continue

            try:
                payload = {"action": "heartbeat"}
                raw = encode(MSG_CONTROL, self.client_id, room, payload)
                
                sock.send_multipart([b"", raw], zmq.NOBLOCK)
                
                try:
                    sock.recv_multipart(zmq.NOBLOCK)
                except zmq.ZMQError:
                    pass
                    
            except (zmq.ZMQError, AttributeError):
                pass

    # ══════════════════════════════════════════════════════════════════════════
    # Internos — descoberta
    # ══════════════════════════════════════════════════════════════════════════

    def _discover_broker(self) -> dict:
        retries = int(os.environ.get("HEARTBEAT_RETRIES", "5"))
        sock    = self.ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, 3000)
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect(REGISTRY_ADDR)

        payload = {"action": "get_broker", "strategy": self.strategy}
        raw     = encode(MSG_CONTROL, self.client_id, "__registry__", payload)

        for attempt in range(1, retries + 1):
            try:
                sock.send(raw)
                resp = decode(sock.recv())
                data = resp.get("data", {})
                if data.get("status") == "ok":
                    sock.close()
                    log.info(
                        "Broker descoberto: %s @ %s",
                        data["broker_id"], data["host"],
                    )
                    return data
                log.warning("Registry respondeu: %s", data)
            except zmq.ZMQError as e:
                log.warning("Tentativa %d/%d de descoberta falhou: %s",
                            attempt, retries, e)
                time.sleep(attempt * 0.5)

        sock.close()
        raise SessionError(
            f"Não foi possível descobrir um broker após {retries} tentativas"
        )