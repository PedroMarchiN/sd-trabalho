"""
broker/broker_cluster.py
─────────────────────────────────────────────────────────────────────────────
Gerencia a comunicação entre brokers do cluster.

Modelo
──────
  Cada broker tem um socket PUB (cluster_pub) e um socket SUB (cluster_sub).
  Ao receber a lista de pares do Registry, conecta o SUB a cada PUB dos pares.

Roteamento entre brokers
────────────────────────
  Quando um cliente publica em broker-A uma mensagem para a sala X,
  e há clientes assinantes da sala X em broker-B, o broker-A precisa
  repassar essa mensagem para broker-B.

  Fluxo:
    [Cliente PUB] → XSUB(broker-A) → proxy → XPUB(broker-A) → [Clientes locais]
                                                         ↓
                                              cluster_pub(broker-A)
                                                         ↓
                                              cluster_sub(broker-B)
                                                         ↓
                                              XSUB(broker-B) [injeção manual]
                                                         ↓
                                              XPUB(broker-B) → [Clientes B]

Anti-loop
─────────
  Cada mensagem recebe o campo "hops": lista de broker_ids já visitados.
  O broker descarta se seu próprio ID já estiver na lista.
"""

import os
import sys
import time
import logging
import threading

import zmq

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.protocol import decode, encode, MSG_CONTROL
from common.channels  import (
    BROKER_ID, BROKER_HOST, BROKER_BASE_PORT,
    broker_ports, REGISTRY_ADDR, HEARTBEAT_INTERVAL,
)

log = logging.getLogger(f"cluster.{BROKER_ID}")


class BrokerCluster:
    """
    Sub-sistema de cluster para um broker.

    Parâmetros
    ──────────
    broker : referência ao Broker pai (acesso a ctx, ports, presence)
    """

    def __init__(self, broker):
        self.broker    = broker
        self.broker_id = broker.broker_id
        self.ctx       = broker.ctx
        self.ports     = broker.ports
        self._running  = False

        # { broker_id: { "host": str, "ports": dict, "ts": float } }
        self._peers: dict[str, dict] = {}
        self._peers_lock = threading.Lock()

        self._pub_sock: zmq.Socket | None = None   # publica para cluster
        self._sub_sock: zmq.Socket | None = None   # assina cluster

    # ── Ciclo de vida ──────────────────────────────────────────────────────────
    def start(self) -> None:
        self._running = True
        self._setup_cluster_sockets()

        threading.Thread(target=self._thread_receive,    daemon=True, name="cluster_recv").start()
        threading.Thread(target=self._thread_peer_sync,  daemon=True, name="cluster_sync").start()

        log.info(
            "Cluster iniciado | pub=%d sub=%d",
            self.ports["cluster_pub"],
            self.ports["cluster_sub"],
        )

    def stop(self) -> None:
        self._running = False

    # ── Setup de sockets ───────────────────────────────────────────────────────
    def _setup_cluster_sockets(self) -> None:
        self._pub_sock = self.ctx.socket(zmq.PUB)
        self._pub_sock.bind(f"tcp://{BROKER_HOST}:{self.ports['cluster_pub']}")

        self._sub_sock = self.ctx.socket(zmq.SUB)
        # Assina TUDO vindo de outros brokers (filtragem pelo campo "hops")
        self._sub_sock.setsockopt(zmq.SUBSCRIBE, b"")

    # ── Gestão de peers ────────────────────────────────────────────────────────
    def add_peer(self, peer_id: str, host: str, ports: dict) -> None:
        """Conecta o SUB ao PUB do peer e registra o peer."""
        if peer_id == self.broker_id:
            return
        with self._peers_lock:
            if peer_id in self._peers:
                return   # já conectado
            addr = f"tcp://{host}:{ports['cluster_pub']}"
            self._sub_sock.connect(addr)
            self._peers[peer_id] = {"host": host, "ports": ports, "ts": time.time()}
            log.info("Peer adicionado: %s @ %s", peer_id, addr)

    def remove_peer(self, peer_id: str) -> None:
        with self._peers_lock:
            peer = self._peers.pop(peer_id, None)
        if peer:
            addr = f"tcp://{peer['host']}:{peer['ports']['cluster_pub']}"
            self._sub_sock.disconnect(addr)
            log.warning("Peer removido: %s", peer_id)

    def peer_alive(self, peer_id: str) -> None:
        """Atualiza timestamp de um peer (heartbeat recebido)."""
        with self._peers_lock:
            if peer_id in self._peers:
                self._peers[peer_id]["ts"] = time.time()

    # ── Publicação para cluster ────────────────────────────────────────────────
    def forward(self, topic: bytes, raw: bytes) -> None:
        """
        Repassa uma mensagem para outros brokers.
        Injeta o broker_id atual em 'hops' para evitar loops.
        """
        try:
            msg = decode(raw)
        except Exception:
            return

        hops: list = msg.get("hops", [])
        if self.broker_id in hops:
            return   # já passou por aqui — descarta
        hops.append(self.broker_id)
        msg["hops"] = hops

        import msgpack
        new_raw = msgpack.packb(msg, use_bin_type=True)
        try:
            self._pub_sock.send_multipart([topic, new_raw], zmq.NOBLOCK)
        except zmq.ZMQError:
            pass   # fila cheia — descarta (QoS para vídeo/áudio)

    # ── Threads ────────────────────────────────────────────────────────────────
    def _thread_receive(self) -> None:
        """
        Recebe mensagens dos peers e injeta no XSUB local
        para que o proxy as redistribua aos clientes deste broker.
        """
        poller = zmq.Poller()
        poller.register(self._sub_sock, zmq.POLLIN)

        while self._running:
            events = dict(poller.poll(timeout=300))
            if self._sub_sock not in events:
                continue
            try:
                frames = self._sub_sock.recv_multipart()
                if len(frames) < 2:
                    continue
                topic_b, raw = frames[0], frames[1]

                msg = decode(raw)

                # ── Heartbeat de peer — apenas atualiza timestamp ──────────
                if topic_b == b"__hb__":
                    sender = msg.get("data", {}).get("broker_id") or msg.get("from")
                    if sender and sender != self.broker_id:
                        self.peer_alive(sender)
                    continue

                # ── Mensagem de mídia — verifica anti-loop e injeta ────────
                if self.broker_id in msg.get("hops", []):
                    continue

                self.broker._frontend.send_multipart([topic_b, raw], zmq.NOBLOCK)

            except zmq.ZMQError as e:
                if self._running:
                    log.debug("cluster recv error: %s", e)
            except Exception as e:
                log.warning("Erro ao processar mensagem de cluster: %s", e)

    def _thread_peer_sync(self) -> None:
        """
        Periodicamente consulta o Registry para descobrir novos peers
        e remove peers inativos (sem heartbeat).
        """
        timeout = float(os.environ.get("HEARTBEAT_TIMEOUT", "5.0"))

        while self._running:
            time.sleep(HEARTBEAT_INTERVAL * 2)
            self._sync_peers_from_registry()
            self._evict_dead_peers(timeout)

    def _sync_peers_from_registry(self) -> None:
        """Busca a lista de brokers ativos no Registry."""
        sock = self.ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, 2000)
        sock.connect(REGISTRY_ADDR)

        try:
            raw = encode(MSG_CONTROL, self.broker_id, "__registry__",
                         {"action": "list_brokers"})
            sock.send(raw)
            resp = decode(sock.recv())
            brokers = resp.get("data", {}).get("brokers", {})
            for bid, info in brokers.items():
                self.add_peer(bid, info["host"], info["ports"])
        except zmq.ZMQError as e:
            log.debug("sync_peers falhou: %s", e)
        finally:
            sock.close()

    def _evict_dead_peers(self, timeout: float) -> None:
        now = time.time()
        with self._peers_lock:
            dead = [
                pid for pid, info in self._peers.items()
                if now - info["ts"] > timeout * 5
            ]
        for pid in dead:
            log.warning("Peer %s sem heartbeat — removendo", pid)
            self.remove_peer(pid)