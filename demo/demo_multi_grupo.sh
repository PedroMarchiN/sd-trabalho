#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# demo/demo_multi_grupo.sh
# Demonstra múltiplos grupos simultâneos: salas A, B e C em paralelo.
#
# Pré-requisito: infraestrutura Docker rodando
#   docker compose up --build registry broker-1 broker-2 broker-3
#
# Uso:
#   bash demo/demo_multi_grupo.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")/.."

echo -e "\n\033[1m── Demo: Múltiplos Grupos Simultâneos ──────────────────\033[0m\n"

python3 -c "
import os, sys, time, threading
sys.path.insert(0, '.')
from common.protocol import MSG_TEXT
from client.session import Session

results = {}   # sala -> mensagens recebidas
lock = threading.Lock()

# Cria 2 usuários por sala: A, B, C
grupos = [('A', 'alice-A', 'bob-A'), ('B', 'alice-B', 'bob-B'), ('C', 'alice-C', 'bob-C')]
sessions = []

for sala, u1, u2 in grupos:
    s1 = Session(u1)
    s2 = Session(u2)
    s1.connect()
    s2.connect()
    s1.join(sala)
    s2.join(sala)
    results[sala] = []

    def make_cb(s):
        def cb(msg):
            with lock:
                results[s].append(msg.get('data', ''))
        return cb

    s2.subscribe(sala, MSG_TEXT, make_cb(sala))
    sessions.append((sala, s1, s2))
    print(f'  · Sala {sala}: {u1} → {s1.broker_info[\"broker_id\"]}  |  {u2} → {s2.broker_info[\"broker_id\"]}')

time.sleep(0.5)

# Todos enviam simultaneamente
threads = []
for sala, s1, s2 in sessions:
    msg = f'Olá sala {sala}!'
    t = threading.Thread(target=lambda s=s1, m=msg: s.publish(MSG_TEXT, m))
    threads.append((sala, msg, t))
    t.start()
for _, _, t in threads:
    t.join()

time.sleep(2.0)

print()
all_ok = True
with lock:
    for sala, msg, _ in threads:
        got = results[sala]
        if msg in got:
            print(f'  \033[32m✓\033[0m Sala {sala}: mensagem entregue')
        else:
            print(f'  \033[31m✗\033[0m Sala {sala}: NÃO entregue (got={got})')
            all_ok = False

for _, s1, s2 in sessions:
    s1.disconnect()
    s2.disconnect()

if all_ok:
    print('\n  \033[32m✓\033[0m Todos os grupos funcionando simultaneamente')
"
echo -e "\n\033[1m── Fim da demonstração Multi-Grupo ─────────────────────\033[0m\n"
