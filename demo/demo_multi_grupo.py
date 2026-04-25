"""
demo/demo_multi_grupo.py
─────────────────────────────────────────────────────────────────────────────
Demonstra múltiplos grupos (salas A, B, C) operando simultaneamente,
com clientes distribuídos entre brokers — sem Docker.

Uso:
  python3 demo/demo_multi_grupo.py
"""

import os, sys, time, threading, subprocess, signal

os.environ["REGISTRY_HOST"]      = "127.0.0.1"
os.environ["REGISTRY_PORT"]      = "5582"
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

procs   = []
log_dir = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(log_dir, exist_ok=True)

def start_service(name, env_extra, module):
    env = os.environ.copy()
    env.update(env_extra)
    lf = open(os.path.join(log_dir, f"mg_{name}.log"), "w")
    p = subprocess.Popen([sys.executable, "-m", module], env=env, cwd=ROOT,
                         stdout=lf, stderr=lf)
    procs.append((p, lf))
    return p

def kill_all():
    for p, lf in procs:
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

if __name__ == "__main__":
    print(f"\n\033[1m── Demo: Múltiplos Grupos Simultâneos ──────────────────\033[0m\n")
    print(f"  {INFO} Subindo registry e 3 brokers localmente…")

    cleanup_ports(5582,
                  5640, 5641, 5642, 5643, 5644,
                  5650, 5651, 5652, 5653, 5654,
                  5660, 5661, 5662, 5663, 5664)
    time.sleep(0.3)

    try:
        start_service("registry", {"REGISTRY_PORT": "5582", "BROKER_HOST": "0.0.0.0"}, "registry.registry")
        time.sleep(1.2)

        for bid, base in [("mg-1", 5640), ("mg-2", 5650), ("mg-3", 5660)]:
            start_service(bid, {
                "BROKER_ID": bid, "BROKER_BASE_PORT": str(base),
                "BROKER_HOST": "0.0.0.0", "BROKER_ADVERTISE_HOST": "127.0.0.1",
            }, "broker.broker")

        time.sleep(3.0)

        # Cria pares de clientes por sala: A, B, C
        grupos = [("A", "alice-A", "bob-A"),
                  ("B", "alice-B", "bob-B"),
                  ("C", "alice-C", "bob-C")]

        results = {}   # sala → lista de mensagens recebidas
        lock    = threading.Lock()
        sessions = []

        for sala, u1, u2 in grupos:
            s1 = Session(u1, strategy="round_robin")
            s2 = Session(u2, strategy="round_robin")
            s1.connect(); s2.connect()
            s1.join(sala); s2.join(sala)
            results[sala] = []

            def make_cb(s=sala):
                def cb(msg):
                    with lock:
                        results[s].append(msg.get("data", ""))
                return cb

            s2.subscribe(sala, MSG_TEXT, make_cb())
            sessions.append((sala, s1, s2))
            b1 = s1.broker_info.get("broker_id", "?")
            b2 = s2.broker_info.get("broker_id", "?")
            print(f"  {INFO} Sala {sala}: {u1}@{b1}  ↔  {u2}@{b2}")

        time.sleep(0.5)
        print()

        # Todos enviam simultaneamente
        msgs = {}
        threads = []
        for sala, s1, _ in sessions:
            m = f"Mensagem simultânea para sala {sala}"
            msgs[sala] = m
            t = threading.Thread(target=lambda s=s1, msg=m: s.publish(MSG_TEXT, msg))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        print(f"  {INFO} 3 mensagens enviadas simultaneamente (salas A, B, C)\n")

        time.sleep(2.0)

        all_ok = True
        with lock:
            for sala, _, _ in sessions:
                got = results[sala]
                if msgs[sala] in got:
                    print(f"  {PASS} Sala {sala}: mensagem entregue")
                else:
                    print(f"  {FAIL} Sala {sala}: NÃO entregue (got={got})")
                    all_ok = False

        print()
        if all_ok:
            print(f"  {PASS} Todos os 3 grupos funcionando simultaneamente")

        for _, s1, s2 in sessions:
            s1.disconnect(); s2.disconnect()

    finally:
        print(f"\n  {INFO} Encerrando processos…")
        kill_all()
        time.sleep(0.3)

    print(f"\n\033[1m── Fim da demonstração Multi-Grupo ─────────────────────\033[0m\n")
