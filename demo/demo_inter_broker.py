"""
demo/demo_inter_broker.py
─────────────────────────────────────────────────────────────────────────────
Demonstra comunicação entre clientes em brokers distintos.
Sobe registry + 2 brokers como subprocessos Python (sem Docker).

Uso:
  python3 demo/demo_inter_broker.py
"""

import os, sys, time, threading, subprocess, signal

os.environ["REGISTRY_HOST"]      = "127.0.0.1"
os.environ["REGISTRY_PORT"]      = "5580"        # porta separada para não colidir
os.environ["HEARTBEAT_INTERVAL"] = "1.0"
os.environ["HEARTBEAT_TIMEOUT"]  = "4.0"
os.environ["HEARTBEAT_RETRIES"]  = "3"

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from common.protocol import MSG_TEXT
from client.session import Session

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
INFO = "\033[36m·\033[0m"

# ── Gestão de processos ────────────────────────────────────────────────────────
procs = []
log_dir = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(log_dir, exist_ok=True)

def start_service(name, env_extra, module):
    env = os.environ.copy()
    env.update(env_extra)
    lf = open(os.path.join(log_dir, f"ib_{name}.log"), "w")
    p = subprocess.Popen([sys.executable, "-m", module], env=env, cwd=ROOT,
                         stdout=lf, stderr=lf)
    procs.append((name, p, lf))
    return p

def kill_all():
    for _, p, lf in procs:
        p.terminate()
        try: p.wait(timeout=2)
        except subprocess.TimeoutExpired: p.kill()
        try: lf.close()
        except: pass

def cleanup_ports(*ports):
    for port in ports:
        try:
            out = subprocess.check_output(["lsof", "-ti", f":{port}"], text=True).strip()
            for pid in out.split():
                try: os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError: pass
        except subprocess.CalledProcessError:
            pass

# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n\033[1m── Demo: Comunicação Inter-Broker ──────────────────────\033[0m\n")
    print(f"  {INFO} Subindo registry e 2 brokers localmente…")

    cleanup_ports(5580, 5590, 5591, 5592, 5593, 5594, 5600, 5601, 5602, 5603, 5604)
    time.sleep(0.3)

    try:
        start_service("registry", {"REGISTRY_PORT": "5580", "BROKER_HOST": "0.0.0.0"}, "registry.registry")
        time.sleep(1.2)
        start_service("broker-A", {
            "BROKER_ID": "broker-A", "BROKER_BASE_PORT": "5590",
            "BROKER_HOST": "0.0.0.0", "BROKER_ADVERTISE_HOST": "127.0.0.1",
        }, "broker.broker")
        start_service("broker-B", {
            "BROKER_ID": "broker-B", "BROKER_BASE_PORT": "5600",
            "BROKER_HOST": "0.0.0.0", "BROKER_ADVERTISE_HOST": "127.0.0.1",
        }, "broker.broker")
        time.sleep(3.0)

        received = []
        lock = threading.Lock()

        alice = Session("alice-demo", strategy="round_robin")
        bob   = Session("bob-demo",   strategy="round_robin")
        alice.connect()
        bob.connect()

        b_alice = alice.broker_info.get("broker_id", "?")
        b_bob   = bob.broker_info.get("broker_id", "?")
        print(f"  {INFO} alice → {b_alice}")
        print(f"  {INFO} bob   → {b_bob}")

        alice.join("C")
        bob.join("C")

        def on_msg(msg):
            with lock:
                received.append(msg.get("data", ""))

        bob.subscribe("C", MSG_TEXT, on_msg)
        time.sleep(0.5)

        texto = "Olá bob! Mensagem cross-broker de alice."
        alice.publish(MSG_TEXT, texto)
        print(f"  {INFO} alice enviou: \"{texto}\"")
        time.sleep(1.5)

        with lock:
            got = list(received)

        print()
        if b_alice != b_bob:
            if texto in got:
                print(f"  {PASS} bob recebeu a mensagem (inter-broker confirmado)")
            else:
                print(f"  {FAIL} bob NÃO recebeu (msgs={got})")
        else:
            if texto in got:
                print(f"  {PASS} mensagem entregue (ambos no mesmo broker desta vez — tente novamente)")
            else:
                print(f"  {FAIL} falha na entrega")

        alice.disconnect()
        bob.disconnect()

    finally:
        print(f"\n  {INFO} Encerrando processos…")
        kill_all()
        time.sleep(0.3)

    print(f"\n\033[1m── Fim da demonstração Inter-Broker ────────────────────\033[0m\n")
