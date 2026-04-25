"""
broker/broker.py
─────────────────────────────────────────────────────────────────────────────
Broker principal de mensagens.

Responsabilidades
─────────────────
1. Receber publicações dos clientes (XSUB frontend)
2. Redistribuir para assinantes (XPUB backend)         
3. Atender comandos de controle: join/leave/heartbeat   
4. Repassar mensagens para outros brokers do cluster    
5. Registrar-se no Registry ao iniciar

Portas (configuráveis via env BROKER_BASE_PORT, default 5555)
──────────────────────────────────────────────────────────────
  base+0  XSUB   frontend   ← clientes publicam aqui
  base+1  XPUB   backend    → clientes assinam aqui
  base+2  ROUTER control    ← join/leave/heartbeat
  base+3  PUB    cluster    → outros brokers
  base+4  SUB    cluster    ← outros brokers

Diagrama de fluxo
──────────────────
  [Cliente PUB] → XSUB:5555 ──proxy──► XPUB:5556 → [Cliente SUB]
                                   ↕
                              cluster PUB/SUB (inter-broker)
"""

import os
import sys
import time
import threading
import logging
import queue
import zmq

# Adiciona o diretório raiz ao path para importar common.*
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.protocol import decode, encode, MSG_CONTROL, CTRL_JOIN, CTRL_LEAVE, CTRL_ACK
from common.channels  import (
    BROKER_ID, BROKER_HOST, BROKER_BASE_PORT, broker_ports,
    REGISTRY_ADDR, HEARTBEAT_INTERVAL,
)
from broker.heartbeat      import HeartbeatManager
from broker.broker_cluster import BrokerCluster

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(f"broker.{BROKER_ID}")


# ══════════════════════════════════════════════════════════════════════════════
class PresenceManager:
    """
    Mantém o estado de presença: quais clientes estão em quais salas.
    Thread-safe via lock.
    """

    def __init__(self):
        self._lock   = threading.Lock()
        # { room: { client_id: { "ts": float, "addr": str } } }
        self._rooms: dict[str, dict] = {}

    # ── Escrita ────────────────────────────────────────────────────────────────
    def join(self, room: str, client_id: str, addr: str = "") -> None:
        with self._lock:
            self._rooms.setdefault(room, {})[client_id] = {
                "ts": time.time(), "addr": addr
            }
        log.info("JOIN  room=%-6s client=%s", room, client_id)

    def leave(self, room: str, client_id: str) -> None:
        with self._lock:
            if room in self._rooms:
                self._rooms[room].pop(client_id, None)
                if not self._rooms[room]:
                    del self._rooms[room]
        log.info("LEAVE room=%-6s client=%s", room, client_id)

    def touch(self, client_id: str) -> None:
        """Atualiza timestamp de heartbeat de um cliente."""
        with self._lock:
            for room_clients in self._rooms.values():
                if client_id in room_clients:
                    room_clients[client_id]["ts"] = time.time()

    # ── Leitura ────────────────────────────────────────────────────────────────
    def members(self, room: str) -> list[str]:
        with self._lock:
            return list(self._rooms.get(room, {}).keys())

    def all_rooms(self) -> dict:
        with self._lock:
            return {r: list(c.keys()) for r, c in self._rooms.items()}

    def evict_stale(self, timeout: float) -> list[tuple[str, str]]:
        """Remove clientes sem heartbeat recente. Retorna lista (room, client)."""
        now     = time.time()
        evicted = []
        with self._lock:
            for room, clients in list(self._rooms.items()):
                for cid, info in list(clients.items()):
                    if now - info["ts"] > timeout:
                        del clients[cid]
                        evicted.append((room, cid))
                if not clients:
                    del self._rooms[room]
        for room, cid in evicted:
            log.warning("EVICT client=%s room=%s (timeout)", cid, room)
        return evicted


# ══════════════════════════════════════════════════════════════════════════════
class Broker:
    """
    Broker central de mensagens.

    Threads internas
    ─────────────────
    • _thread_proxy    — redistribuição e interligação com cluster
    • _thread_control  — loop ROUTER para join/leave/hb
    • _thread_evict    — remove clientes inativos periodicamente
    """

    def __init__(self):
        self.broker_id = BROKER_ID
        self.ports     = broker_ports(BROKER_BASE_PORT)
        self.ctx       = zmq.Context.instance()
        self.presence  = PresenceManager()
        self._running  = False

        # Sockets (criados em start para ficarem na thread correta)
        self._frontend: zmq.Socket | None = None
        self._backend:  zmq.Socket | None = None
        self._control:  zmq.Socket | None = None

        # Sub-sistemas
        self.cluster   = BrokerCluster(self)
        self.heartbeat = HeartbeatManager(self)

    # ── Ciclo de vida ──────────────────────────────────────────────────────────
    def start(self) -> None:
        self._running = True
        self._setup_sockets()
        self._register_on_registry()

        threads = [
            threading.Thread(target=self._thread_proxy,   daemon=True, name="proxy"),
            threading.Thread(target=self._thread_control, daemon=True, name="control"),
            threading.Thread(target=self._thread_evict,   daemon=True, name="evict"),
        ]
        for t in threads:
            t.start()

        self.cluster.start()
        self.heartbeat.start()

        log.info(
            "Broker %s pronto | frontend=%d backend=%d control=%d",
            self.broker_id,
            self.ports["frontend"],
            self.ports["backend"],
            self.ports["control"],
        )

        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            log.info("Encerrando broker %s …", self.broker_id)
            self.stop()

    def stop(self) -> None:
        self._running = False
        self.cluster.stop()
        self.heartbeat.stop()
        self.ctx.term()

    # ── Setup de sockets ───────────────────────────────────────────────────────
    def _setup_sockets(self) -> None:
        # XSUB — recebe publicações dos clientes
        self._frontend = self.ctx.socket(zmq.XSUB)
        self._frontend.bind(f"tcp://{BROKER_HOST}:{self.ports['frontend']}")
        # Assinatura universal: XSUB só entrega mensagens que casam com uma
        # subscrição ativa. Sem isso, quando não há assinante local para um
        # tópico, o XSUB descarta a mensagem e cluster.forward() nunca é chamado.
        self._frontend.send(b"\x01")

        # XPUB — distribui para assinantes
        self._backend = self.ctx.socket(zmq.XPUB)
        self._backend.bind(f"tcp://{BROKER_HOST}:{self.ports['backend']}")
        # Envia cada mensagem de subscrição/desinscrição ao frontend
        self._backend.setsockopt(zmq.XPUB_VERBOSE, 1)

        # ROUTER — controle (join/leave/heartbeat)
        self._control = self.ctx.socket(zmq.ROUTER)
        self._control.bind(f"tcp://{BROKER_HOST}:{self.ports['control']}")

    # ── Threads ────────────────────────────────────────────────────────────────
    def _thread_proxy(self) -> None:
        """
        Redistribuição PUB/SUB manual (substitui zmq.proxy).
        Lê eventos, distribui localmente, envia para o cluster e 
        também injeta mensagens vindas do cluster inter-broker.
        """
        log.debug("proxy thread iniciada")
        poller = zmq.Poller()
        poller.register(self._frontend, zmq.POLLIN)
        poller.register(self._backend, zmq.POLLIN)

        while self._running:
            events = dict(poller.poll(timeout=10))

            try:
                # 1. Mensagens publicadas pelos clientes deste broker
                if self._frontend in events:
                    frames = self._frontend.recv_multipart()
                    self._backend.send_multipart(frames)
                    if len(frames) >= 2:
                        self.cluster.forward(frames[0], frames[1])

                # 2. Inscrições (XPUB -> XSUB)
                if self._backend in events:
                    frames = self._backend.recv_multipart()
                    self._frontend.send_multipart(frames)

                # 3. Mensagens vindas de outros brokers do cluster
                while not self.cluster.inbox.empty():
                    frames = self.cluster.inbox.get_nowait()
                    self._backend.send_multipart(frames)

            except zmq.ZMQError as e:
                if not self._running:
                    break
                log.error("Erro no proxy: %s", e)
            except Exception as e:
                if self._running:
                    log.error("Erro inesperado no proxy: %s", e)

    def _thread_control(self) -> None:
        """Processa mensagens de controle: join / leave / heartbeat."""
        log.debug("control thread iniciada")
        poller = zmq.Poller()
        poller.register(self._control, zmq.POLLIN)

        while self._running:
            events = dict(poller.poll(timeout=500))
            if self._control not in events:
                continue
            try:
                frames = self._control.recv_multipart()
                # ROUTER entrega: [identity, empty, payload]
                if len(frames) < 3:
                    continue
                identity, _, raw = frames[0], frames[1], frames[2]
                self._handle_control(identity, raw)
            except zmq.ZMQError as e:
                if self._running:
                    log.error("control recv error: %s", e)

    def _thread_evict(self) -> None:
        """Remove clientes sem heartbeat a cada HEARTBEAT_INTERVAL segundos."""
        timeout = float(os.environ.get("HEARTBEAT_TIMEOUT", "5.0"))
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL)
            self.presence.evict_stale(timeout)

    # ── Lógica de controle ─────────────────────────────────────────────────────
    def _handle_control(self, identity: bytes, raw: bytes) -> None:
        try:
            msg = decode(raw)
        except Exception as e:
            log.warning("Mensagem de controle inválida: %s", e)
            return

        ctrl_type  = msg.get("data", {})
        if isinstance(ctrl_type, dict):
            action = ctrl_type.get("action", "")
        else:
            action = str(ctrl_type)

        client_id = msg.get("from", "unknown")
        room      = msg.get("room", "")

        if action == CTRL_JOIN:
            self.presence.join(room, client_id)
            self._send_ack(identity, client_id, room, "joined")

        elif action == CTRL_LEAVE:
            self.presence.leave(room, client_id)
            self._send_ack(identity, client_id, room, "left")

        elif action == "heartbeat":
            self.presence.touch(client_id)
            self._send_ack(identity, client_id, room, "hb_ok")

        elif action == "list_rooms":
            # Merge salas locais + remotas do cluster
            local  = self.presence.all_rooms()
            remote = self.cluster.get_all_remote_rooms()
            merged = dict(local)
            for r, members in remote.items():
                if r in merged:
                    merged[r] = sorted(set(merged[r]) | set(members))
                else:
                    merged[r] = members
            self._send_ack(identity, client_id, room, "rooms", extra={"rooms": merged})

        elif action == "who":
            local_members = self.presence.members(room)
            remote_members = self.cluster.get_remote_users(room)
            all_members = sorted(list(set(local_members + remote_members)))
            self._send_ack(identity, client_id, room, "who_resp", extra={"members": all_members})

        else:
            log.debug("Ação de controle desconhecida: %s", action)

    def _send_ack(self, identity: bytes, client_id: str,
                  room: str, status: str, extra: dict = None) -> None:
        payload = {"action": CTRL_ACK, "status": status}
        if extra:
            payload.update(extra)
        raw = encode(MSG_CONTROL, self.broker_id, room, payload)
        try:
            self._control.send_multipart([identity, b"", raw])
        except zmq.ZMQError as e:
            log.warning("Falha ao enviar ACK para %s: %s", client_id, e)

    # ── Registro no Registry ───────────────────────────────────────────────────
    def _register_on_registry(self) -> None:
        """
        Envia os metadados do broker para o registry via REQ/REP.
        Tenta até 5 vezes com backoff.
        """
        sock = self.ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, 3000)
        sock.connect(REGISTRY_ADDR)

        payload = {
            "action":    "register",
            "broker_id": self.broker_id,
            "host":      os.environ.get("BROKER_ADVERTISE_HOST", BROKER_HOST),
            "ports":     self.ports,
        }
        raw = encode(MSG_CONTROL, self.broker_id, "__registry__", payload)

        for attempt in range(1, 6):
            try:
                sock.send(raw)
                resp_raw = sock.recv()
                resp     = decode(resp_raw)
                log.info("Registrado no registry: %s", resp.get("data"))
                break
            except zmq.ZMQError as e:
                log.warning("Tentativa %d/5 de registro falhou: %s", attempt, e)
                time.sleep(attempt * 0.5)
        else:
            log.error("Não foi possível registrar no registry após 5 tentativas.")

        sock.close()


# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    Broker().start()