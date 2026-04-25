"""
demo/test_phase0.py
─────────────────────────────────────────────────────────────────────────────
Teste automatizado da FASE 0 — valida o sistema atual sem Docker.

Sobe:
  • 1 registry (porta 5550)
  • 3 brokers (portas 5555/5565/5575)

Testa:
  ✓ Cliente descobre broker via registry
  ✓ Cliente entra em sala e envia texto
  ✓ Segundo cliente recebe texto na mesma sala
  ✓ Clientes em brokers diferentes se comunicam (inter-broker)
  ✓ Failover: mata broker-1, cliente reconecta automaticamente

Uso:
  python3 demo/test_phase0.py
"""

import os
import sys
import time
import threading
import subprocess
import signal

# ── env vars ANTES de qualquer import de common/client (channels.py lê em import) ──
REGISTRY_HOST = "127.0.0.1"
REGISTRY_PORT = 5550
os.environ["REGISTRY_HOST"]    = REGISTRY_HOST
os.environ["REGISTRY_PORT"]    = str(REGISTRY_PORT)
os.environ["HEARTBEAT_INTERVAL"] = "1.0"
os.environ["HEARTBEAT_TIMEOUT"]  = "4.0"
os.environ["HEARTBEAT_RETRIES"]  = "3"

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from common.protocol import encode, decode, MSG_TEXT, MSG_CONTROL, CTRL_JOIN
from client.session import Session, SessionError

# ─────────────────────────────────────────────────────────────────────────────
PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
INFO = "\033[36m·\033[0m"
results = []


def log(sym, msg):
    print(f"  {sym} {msg}")


def check(name, condition, detail=""):
    if condition:
        log(PASS, name)
        results.append(("PASS", name))
    else:
        log(FAIL, f"{name}" + (f" — {detail}" if detail else ""))
        results.append(("FAIL", name))
    return condition


# ─────────────────────────────────────────────────────────────────────────────
# Gestão de processos
# ─────────────────────────────────────────────────────────────────────────────
procs = []


_log_dir = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(_log_dir, exist_ok=True)


def start_service(name, env_extra, module):
    env = os.environ.copy()
    env.update(env_extra)
    log_file = open(os.path.join(_log_dir, f"{name}.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", module],
        env=env,
        cwd=ROOT,
        stdout=log_file,
        stderr=log_file,
    )
    procs.append((name, proc, log_file))
    return proc


def kill_all():
    for item in procs:
        name, proc, lf = item
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            lf.close()
        except Exception:
            pass


def kill_proc(name):
    for item in procs:
        n, proc, lf = item
        if n == name:
            proc.terminate()
            return proc


# ─────────────────────────────────────────────────────────────────────────────
def cleanup_ports(*ports):
    """Mata processos ocupando as portas antes de subir novos serviços."""
    import subprocess as _sp
    for port in ports:
        try:
            out = _sp.check_output(["lsof", "-ti", f":{port}"], text=True).strip()
            for pid in out.split():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except _sp.CalledProcessError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
def run_tests():
    print("\n\033[1m── Fase 0 — Validação do sistema base ─────────────────────────\033[0m\n")

    # ── 0. Limpa portas de execuções anteriores ────────────────────────────────
    cleanup_ports(
        REGISTRY_PORT,
        5555, 5556, 5557, 5558, 5559,
        5565, 5566, 5567, 5568, 5569,
        5575, 5576, 5577, 5578, 5579,
    )
    time.sleep(0.5)

    # ── 1. Sobe serviços ───────────────────────────────────────────────────────
    print(f"  {INFO} Iniciando registry e 3 brokers...")

    start_service("registry", {
        "REGISTRY_PORT":     str(REGISTRY_PORT),
        "BROKER_HOST":       "0.0.0.0",
        "HEARTBEAT_TIMEOUT": "4.0",
    }, "registry.registry")
    time.sleep(1.5)   # aguarda registry subir

    for bid, base in [("broker-1", 5555), ("broker-2", 5565), ("broker-3", 5575)]:
        start_service(bid, {
            "BROKER_ID":               bid,
            "BROKER_BASE_PORT":        str(base),
            "BROKER_HOST":             "0.0.0.0",
            "BROKER_ADVERTISE_HOST":   "127.0.0.1",
            "REGISTRY_HOST":           REGISTRY_HOST,
            "REGISTRY_PORT":           str(REGISTRY_PORT),
            "HEARTBEAT_INTERVAL":      "1.0",
            "HEARTBEAT_TIMEOUT":       "4.0",
        }, "broker.broker")

    time.sleep(3.0)   # aguarda brokers registrarem no registry e sincronizarem cluster

    # ── 2. Descoberta de broker ────────────────────────────────────────────────
    print(f"\n  {INFO} Teste 1: Service Discovery")
    try:
        s = Session("test-discovery", strategy="round_robin")
        s.connect()
        check("Cliente descobre broker via registry", s.broker_info is not None,
              str(s.broker_info))
        check("Broker retorna host e portas",
              bool(s.broker_info.get("host")) and bool(s.broker_info.get("ports")))
        s.disconnect()
    except SessionError as e:
        check("Cliente descobre broker via registry", False, str(e))
        check("Broker retorna host e portas", False, "skipped")

    # ── 3. Join e envio de texto ───────────────────────────────────────────────
    print(f"\n  {INFO} Teste 2: Chat de texto (mesma sala, mesmo broker)")
    received_msgs = []
    lock = threading.Lock()

    def on_text(msg):
        with lock:
            received_msgs.append(msg.get("data", ""))

    try:
        sender   = Session("alice")
        receiver = Session("bob")

        sender.connect()
        receiver.connect()

        joined_s = sender.join("A")
        joined_r = receiver.join("A")

        check("alice entra na sala A", joined_s)
        check("bob entra na sala A", joined_r)

        receiver.subscribe("A", MSG_TEXT, on_text)
        time.sleep(0.3)   # aguarda sub propagar

        sender.publish(MSG_TEXT, "oi bob, sou alice")
        time.sleep(1.5)   # inclui latência inter-broker se em brokers distintos

        with lock:
            got = received_msgs.copy()
        check("bob recebe texto de alice", "oi bob, sou alice" in got,
              f"recebido: {got}")

        # Verifica que alice não recebe a própria mensagem (filtro from==client_id)
        self_msgs = []
        def on_alice_text(msg):
            with lock:
                self_msgs.append(msg.get("data", ""))
        sender.subscribe("A", MSG_TEXT, on_alice_text)
        sender.publish(MSG_TEXT, "mensagem propria")
        time.sleep(0.3)
        with lock:
            check("alice não recebe a própria msg", "mensagem propria" not in self_msgs,
                  f"self_msgs: {self_msgs}")

        sender.disconnect()
        receiver.disconnect()

    except Exception as e:
        check("alice entra na sala A", False, str(e))

    # ── 4. /who e /rooms ──────────────────────────────────────────────────────
    print(f"\n  {INFO} Teste 3: Presença (/who, /rooms)")
    try:
        s1 = Session("carol")
        s2 = Session("dave")
        s1.connect()
        s2.connect()
        s1.join("B")
        s2.join("B")
        time.sleep(1.5)   # aguarda heartbeat propagar presença

        members = s1.who("B")
        rooms   = s1.list_rooms().get("rooms", {})

        check("/who retorna membros da sala B", "carol" in members and "dave" in members,
              f"membros: {members}")
        check("/rooms lista sala B", "B" in rooms, f"rooms: {rooms}")

        s1.disconnect()
        s2.disconnect()

    except Exception as e:
        check("/who retorna membros da sala B", False, str(e))
        check("/rooms lista sala B", False, "skipped")

    # ── 5. Comunicação inter-broker ────────────────────────────────────────────
    print(f"\n  {INFO} Teste 4: Comunicação inter-broker (salas em brokers distintos)")
    cross_msgs = []
    lock2 = threading.Lock()

    try:
        # broker-1 : 5555, broker-2 : 5565 — forçar via estratégia não é direto;
        # com round_robin os dois irão para brokers diferentes se já houver 1 por broker.
        # Damos tempo para cluster sincronizar.
        time.sleep(2.5)

        peer1 = Session("peer-x")
        peer2 = Session("peer-y")
        peer1.connect()
        peer2.connect()

        log(INFO, f"peer-x → broker {peer1.broker_info.get('broker_id')}")
        log(INFO, f"peer-y → broker {peer2.broker_info.get('broker_id')}")

        peer1.join("C")
        peer2.join("C")

        def on_cross(msg):
            with lock2:
                cross_msgs.append(msg.get("data", ""))

        peer2.subscribe("C", MSG_TEXT, on_cross)
        time.sleep(0.4)

        peer1.publish(MSG_TEXT, "mensagem cross-broker")
        time.sleep(1.5)   # cluster tem latência extra

        with lock2:
            got_cross = cross_msgs.copy()

        brokers_different = (peer1.broker_info.get("broker_id") !=
                             peer2.broker_info.get("broker_id"))

        if brokers_different:
            check("peer-x e peer-y em brokers distintos", True)
            check("peer-y recebe msg de peer-x (inter-broker)",
                  "mensagem cross-broker" in got_cross, f"msgs: {got_cross}")
        else:
            log(INFO, "ambos no mesmo broker (round-robin pode fazer isso) — pulando inter-broker")
            check("peer-x e peer-y em brokers distintos", False,
                  "round-robin colocou no mesmo — tente com mais sessões")

        peer1.disconnect()
        peer2.disconnect()

    except Exception as e:
        check("peer-x e peer-y em brokers distintos", False, str(e))

    # ── 6. Failover ───────────────────────────────────────────────────────────
    print(f"\n  {INFO} Teste 5: Failover automático (mata broker-1)")
    try:
        # Conecta ao broker-1 especificamente via strategy
        fo_client = Session("failover-user", strategy="round_robin")
        fo_client.connect()

        original_broker = fo_client.broker_info.get("broker_id")
        log(INFO, f"conectado em: {original_broker}")
        fo_client.join("D")

        # Aguarda um heartbeat de registro de presença
        time.sleep(2.0)

        # Mata o broker em que o cliente está
        kill_proc(original_broker)
        log(INFO, f"broker {original_broker} encerrado — aguardando failover…")

        # Aguarda failover (até 3 heartbeats × 1s + overhead)
        time.sleep(6.0)

        new_broker = fo_client.broker_info.get("broker_id") if fo_client.broker_info else None
        reconnected = new_broker != original_broker and fo_client._connected
        check(f"cliente reconecta após falha de {original_broker}",
              reconnected,
              f"novo broker: {new_broker}, connected: {fo_client._connected}")
        check("sala D restaurada após failover",
              fo_client.current_room == "D",
              f"sala atual: {fo_client.current_room}")

        fo_client.disconnect()

    except Exception as e:
        check("cliente reconecta após falha de broker", False, str(e))
        check("sala D restaurada após failover", False, "skipped")

    # ─── Resumo ────────────────────────────────────────────────────────────────
    total  = len(results)
    passed = sum(1 for r, _ in results if r == "PASS")
    failed = total - passed

    print(f"\n{'─'*60}")
    print(f"  Resultado: \033[32m{passed} PASS\033[0m  \033[31m{failed} FAIL\033[0m  (total {total})")

    if failed:
        print("\n  Falhas:")
        for r, name in results:
            if r == "FAIL":
                print(f"    {FAIL} {name}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        run_tests()
    finally:
        print("  · Encerrando processos…")
        kill_all()
        time.sleep(0.5)
