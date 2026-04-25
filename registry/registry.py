"""
registry/registry.py
─────────────────────────────────────────────────────────────────────────────
Service Discovery — entidade centralizada leve.

Responsabilidades
─────────────────
• Receber registros de brokers (REP)
• Responder clientes com a lista de brokers disponíveis
• Detectar brokers mortos via timeout de heartbeat
• Sugerir broker por estratégia (round-robin ou menor carga)

Protocolo (REQ → REP)
──────────────────────
  Registro:
    req → { action: "register",    broker_id, host, ports }
    rep ← { status: "ok" }

  Heartbeat:
    req → { action: "heartbeat",   broker_id, ts, clients }
    rep ← { status: "ok" }

  Descoberta (cliente):
    req → { action: "get_broker",  strategy: "round_robin"|"least_load" }
    rep ← { broker_id, host, ports }

  Listagem (inter-broker):
    req → { action: "list_brokers" }
    rep ← { brokers: { id: { host, ports, clients, ts } } }

  Membros de sala (cliente):
    req → { action: "who", room: "A" }
    rep ← { status: "ok", members: ["Nome1", "Nome2"] }

  Todas as salas (cliente):
    req → { action: "list_rooms" }
    rep ← { status: "ok", rooms: { "A": ["Nome1", "Nome2"] } }
"""

import os
import sys
import time
import logging
import threading

import zmq

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.protocol import decode, encode, MSG_CONTROL
from common.channels  import REGISTRY_PORT, BROKER_HOST, HEARTBEAT_TIMEOUT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] registry — %(message)s",
)
log = logging.getLogger("registry")


class Registry:
    """
    Registry de brokers com suporte a heartbeat e service discovery.
    """

    def __init__(self):
        self.ctx      = zmq.Context.instance()
        self._running = False
        self._lock    = threading.Lock()

        # { broker_id: { host, ports, clients, ts } }
        self._brokers: dict[str, dict] = {}

        # Índice para round-robin
        self._rr_index = 0

    # ── Ciclo de vida ──────────────────────────────────────────────────────────
    def start(self) -> None:
        self._running = True

        sock = self.ctx.socket(zmq.REP)
        sock.bind(f"tcp://{BROKER_HOST}:{REGISTRY_PORT}")
        log.info("Registry escutando em tcp://*:%d", REGISTRY_PORT)

        threading.Thread(
            target=self._thread_evict,
            daemon=True,
            name="registry_evict",
        ).start()

        self._loop(sock)

    # ── Loop principal ─────────────────────────────────────────────────────────
    def _loop(self, sock: zmq.Socket) -> None:
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)

        while self._running:
            events = dict(poller.poll(timeout=500))
            if sock not in events:
                continue
            try:
                raw     = sock.recv()
                msg     = decode(raw)
                resp    = self._handle(msg)
                sock.send(encode(MSG_CONTROL, "registry", "__registry__", resp))
            except Exception as e:
                log.error("Erro no loop do registry: %s", e)
                try:
                    sock.send(encode(MSG_CONTROL, "registry", "__registry__",
                                     {"status": "error", "reason": str(e)}))
                except Exception:
                    pass

    # ── Dispatch de ações ──────────────────────────────────────────────────────
    def _handle(self, msg: dict) -> dict:
        data   = msg.get("data", {})
        action = data.get("action", "") if isinstance(data, dict) else ""

        if action == "register":
            return self._handle_register(data)
        elif action == "heartbeat":
            return self._handle_heartbeat(data)
        elif action == "get_broker":
            return self._handle_get_broker(data)
        elif action == "list_brokers":
            return self._handle_list_brokers()
        elif action == "who":
            return self._handle_who(data)
        elif action == "list_rooms":
            return self._handle_list_rooms()
        else:
            log.debug("Ação desconhecida: %s", action)
            return {"status": "unknown_action"}

    def _handle_register(self, data: dict) -> dict:
        bid = data.get("broker_id")
        with self._lock:
            self._brokers[bid] = {
                "host":    data.get("host", "localhost"),
                "ports":   data.get("ports", {}),
                "clients": 0,
                "ts":      time.time(),
                "rooms":   {},   # {room: [members]} — atualizado pelo heartbeat
            }
        log.info("REGISTER broker=%s host=%s", bid, data.get("host"))
        return {"status": "ok", "broker_id": bid}

    def _handle_heartbeat(self, data: dict) -> dict:
        bid = data.get("broker_id")
        with self._lock:
            if bid in self._brokers:
                self._brokers[bid]["ts"]      = time.time()
                self._brokers[bid]["clients"] = data.get("clients", 0)
                # Broker envia {room: [members]} no heartbeat — salva aqui
                # Isso é a fonte da verdade para /who e /rooms
                if "rooms" in data:
                    self._brokers[bid]["rooms"] = data["rooms"]
        return {"status": "ok"}

    def _handle_get_broker(self, data: dict) -> dict:
        strategy = data.get("strategy", "round_robin")
        broker   = self._select_broker(strategy)
        if not broker:
            return {"status": "no_brokers"}
        bid, info = broker
        return {
            "status":    "ok",
            "broker_id": bid,
            "host":      info["host"],
            "ports":     info["ports"],
        }

    def _handle_list_brokers(self) -> dict:
        with self._lock:
            snapshot = {k: dict(v) for k, v in self._brokers.items()}
        return {"status": "ok", "brokers": snapshot}

    def _handle_who(self, data: dict) -> dict:
        """Agrega membros de uma sala de TODOS os brokers vivos."""
        room = data.get("room", "")
        members = set()
        now = time.time()
        timeout = float(os.environ.get("HEARTBEAT_TIMEOUT", str(HEARTBEAT_TIMEOUT)))
        with self._lock:
            for info in self._brokers.values():
                if now - info["ts"] <= timeout:
                    for m in info.get("rooms", {}).get(room, []):
                        members.add(m)
        return {"status": "ok", "members": sorted(members)}

    def _handle_list_rooms(self) -> dict:
        """Agrega todas as salas de todos os brokers vivos."""
        rooms: dict[str, set] = {}
        now = time.time()
        timeout = float(os.environ.get("HEARTBEAT_TIMEOUT", str(HEARTBEAT_TIMEOUT)))
        with self._lock:
            for info in self._brokers.values():
                if now - info["ts"] <= timeout:
                    for room, members in info.get("rooms", {}).items():
                        if room not in rooms:
                            rooms[room] = set()
                        rooms[room].update(members)
        return {"status": "ok", "rooms": {r: sorted(m) for r, m in rooms.items()}}

    # ── Seleção de broker ──────────────────────────────────────────────────────
    def _select_broker(self, strategy: str):
        with self._lock:
            alive = list(self._brokers.items())
        if not alive:
            return None

        if strategy == "least_load":
            return min(alive, key=lambda x: x[1]["clients"])

        # round_robin (default)
        self._rr_index = self._rr_index % len(alive)
        chosen         = alive[self._rr_index]
        self._rr_index = (self._rr_index + 1) % len(alive)
        return chosen

    # ── Eviction de brokers mortos ─────────────────────────────────────────────
    def _thread_evict(self) -> None:
        timeout = float(os.environ.get("HEARTBEAT_TIMEOUT", str(HEARTBEAT_TIMEOUT)))
        while self._running:
            time.sleep(timeout / 2)
            now = time.time()
            with self._lock:
                dead = [
                    bid for bid, info in self._brokers.items()
                    if now - info["ts"] > timeout
                ]
                for bid in dead:
                    del self._brokers[bid]
                    log.warning("EVICT broker=%s (timeout)", bid)


# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    Registry().start()