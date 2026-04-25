"""
demo/demo_failover.py
─────────────────────────────────────────────────────────────────────────────
Demonstra failover automático: derruba o broker do cliente, cliente
reconecta automaticamente e restaura a sala — sem Docker.

Uso:
  python3 demo/demo_failover.py
"""

import os, sys, time, subprocess, signal

os.environ["REGISTRY_HOST"]      = "127.0.0.1"
os.environ["REGISTRY_PORT"]      = "5581"
os.environ["HEARTBEAT_INTERVAL"] = "1.0"
os.environ["HEARTBEAT_TIMEOUT"]  = "4.0"
os.environ["HEARTBEAT_RETRIES"]  = "3"

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from client.session import Session

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
INFO = "\033[36m·\033[0m"

procs   = {}   # nome → (proc, log_file)
log_dir = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(log_dir, exist_ok=True)

def start_service(name, env_extra, module):
    env = os.environ.copy()
    env.update(env_extra)
    lf = open(os.path.join(log_dir, f"fo_{name}.log"), "w")
    p = subprocess.Popen([sys.executable, "-m", module], env=env, cwd=ROOT,
                         stdout=lf, stderr=lf)
    procs[name] = (p, lf)
    return p

def kill_service(name):
    if name in procs:
        p, _ = procs[name]
        p.terminate()
        try: p.wait(timeout=2)
        except subprocess.TimeoutExpired: p.kill()

def kill_all():
    for name in list(procs):
        kill_service(name)
    for _, lf in procs.values():
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
    print(f"\n\033[1m── Demo: Failover Automático ───────────────────────────\033[0m\n")
    print(f"  {INFO} Subindo registry e 3 brokers localmente…")

    cleanup_ports(5581,
                  5610, 5611, 5612, 5613, 5614,
                  5620, 5621, 5622, 5623, 5624,
                  5630, 5631, 5632, 5633, 5634)
    time.sleep(0.3)

    try:
        start_service("registry", {"REGISTRY_PORT": "5581", "BROKER_HOST": "0.0.0.0"}, "registry.registry")
        time.sleep(1.2)

        for bid, base in [("fo-1", 5610), ("fo-2", 5620), ("fo-3", 5630)]:
            start_service(bid, {
                "BROKER_ID": bid, "BROKER_BASE_PORT": str(base),
                "BROKER_HOST": "0.0.0.0", "BROKER_ADVERTISE_HOST": "127.0.0.1",
            }, "broker.broker")

        time.sleep(3.0)

        client = Session("failover-demo", strategy="round_robin")
        client.connect()

        broker_orig = client.broker_info.get("broker_id")
        print(f"  {INFO} Conectado ao broker: \033[1m{broker_orig}\033[0m")

        client.join("D")
        print(f"  {INFO} Entrou na sala D")
        time.sleep(2.0)

        print(f"\n  {INFO} Derrubando {broker_orig}…")
        kill_service(broker_orig)

        print(f"  {INFO} Aguardando detecção e failover (até 15s)…")
        start = time.time()
        while time.time() - start < 15:
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

    finally:
        print(f"\n  {INFO} Encerrando processos…")
        kill_all()
        time.sleep(0.3)

    print(f"\n\033[1m── Fim da demonstração de Failover ─────────────────────\033[0m\n")
