"""
demo/demo_failover_docker.py
─────────────────────────────────────────────────────────────────────────────
Demonstra failover automático usando a infraestrutura Docker.
Derruba o container do broker ativo com `docker compose stop`,
aguarda o cliente reconectar, e depois restaura o container.

Pré-requisito:
  docker compose up registry broker-1 broker-2 broker-3

Uso:
  python3 demo/demo_failover_docker.py
"""

import os, sys, time, subprocess

os.environ["REGISTRY_HOST"]      = "127.0.0.1"
os.environ["REGISTRY_PORT"]      = "5550"
os.environ["HEARTBEAT_INTERVAL"] = "1.0"
os.environ["HEARTBEAT_TIMEOUT"]  = "5.0"
os.environ["HEARTBEAT_RETRIES"]  = "3"

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from client.session import Session

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
INFO = "\033[36m·\033[0m"

def docker_stop(broker_id: str):
    subprocess.run(["docker", "compose", "stop", broker_id],
                   cwd=ROOT, capture_output=True)

def docker_start(broker_id: str):
    subprocess.run(["docker", "compose", "start", broker_id],
                   cwd=ROOT, capture_output=True)

if __name__ == "__main__":
    print(f"\n\033[1m── Demo Docker: Failover Automático ────────────────────\033[0m\n")

    client = Session("failover-demo", strategy="round_robin")
    client.connect()

    broker_orig = client.broker_info.get("broker_id")
    print(f"  {INFO} Conectado ao broker: \033[1m{broker_orig}\033[0m")

    client.join("D")
    print(f"  {INFO} Entrou na sala D")
    time.sleep(2.0)

    print(f"\n  {INFO} Derrubando container {broker_orig} via docker compose stop…")
    docker_stop(broker_orig)

    print(f"  {INFO} Aguardando detecção e failover (até 20s)…")
    start = time.time()
    while time.time() - start < 20:
        time.sleep(0.5)
        if (client.broker_info and
                client.broker_info.get("broker_id") != broker_orig and
                client._connected):
            break

    new_broker = client.broker_info.get("broker_id") if client.broker_info else None
    reconectou = (new_broker and new_broker != broker_orig and client._connected)
    sala_ok    = client.current_room == "D"

    print()
    if reconectou:
        print(f"  {PASS} Reconectado automaticamente → \033[1m{new_broker}\033[0m")
    else:
        print(f"  {FAIL} Failover falhou (broker={new_broker}, connected={client._connected})")

    if sala_ok:
        print(f"  {PASS} Sala D restaurada automaticamente")
    else:
        print(f"  {FAIL} Sala não restaurada (atual: {client.current_room!r})")

    client.disconnect()

    print(f"\n  {INFO} Restaurando container {broker_orig}…")
    docker_start(broker_orig)
    print(f"  {INFO} {broker_orig} restaurado")

    print(f"\n\033[1m── Fim da demonstração de Failover (Docker) ────────────\033[0m\n")
