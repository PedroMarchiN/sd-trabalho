"""
demo/demo_inter_broker_docker.py
─────────────────────────────────────────────────────────────────────────────
Demonstra comunicação entre clientes em brokers distintos.
Usa a infraestrutura Docker já rodando (registry + brokers).

Pré-requisito:
  docker compose up registry broker-1 broker-2 broker-3

Uso:
  python3 demo/demo_inter_broker_docker.py
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
    print(f"\n\033[1m── Demo Docker: Comunicação Inter-Broker ───────────────\033[0m\n")

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
    time.sleep(1.0)

    texto = "Olá bob! Mensagem cross-broker de alice."
    alice.publish(MSG_TEXT, texto)
    print(f"  {INFO} alice enviou: \"{texto}\"")
    time.sleep(2.0)

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

    print(f"\n\033[1m── Fim da demonstração Inter-Broker (Docker) ───────────\033[0m\n")
