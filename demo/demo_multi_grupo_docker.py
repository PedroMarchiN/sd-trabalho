"""
demo/demo_multi_grupo_docker.py
─────────────────────────────────────────────────────────────────────────────
Demonstra múltiplos grupos (salas A, B, C) operando simultaneamente,
com clientes distribuídos entre brokers.
Usa a infraestrutura Docker já rodando (registry + brokers).

Pré-requisito:
  docker compose up registry broker-1 broker-2 broker-3

Uso:
  python3 demo/demo_multi_grupo_docker.py
"""

import os, sys, time, threading

os.environ["REGISTRY_HOST"]      = "127.0.0.1"
os.environ["REGISTRY_PORT"]      = "5550"
os.environ["HEARTBEAT_INTERVAL"] = "1.0"
os.environ["HEARTBEAT_TIMEOUT"]  = "5.0"
os.environ["HEARTBEAT_RETRIES"]  = "3"

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from common.protocol import MSG_TEXT
from client.session import Session

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
INFO = "\033[36m·\033[0m"

if __name__ == "__main__":
    print(f"\n\033[1m── Demo Docker: Múltiplos Grupos Simultâneos ───────────\033[0m\n")

    grupos = [("A", "alice-A", "bob-A"),
              ("B", "alice-B", "bob-B"),
              ("C", "alice-C", "bob-C")]

    results  = {}
    lock     = threading.Lock()
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

    time.sleep(1.0)
    print()

    msgs    = {}
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

    print(f"\n\033[1m── Fim da demonstração Multi-Grupo (Docker) ────────────\033[0m\n")
