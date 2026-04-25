#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# demo/demo_inter_broker.sh
# Demonstra comunicação entre clientes em brokers distintos.
#
# Pré-requisito: infraestrutura Docker rodando
#   docker compose up --build registry broker-1 broker-2 broker-3
#
# Uso:
#   bash demo/demo_inter_broker.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")/.."

INFO="\033[36m·\033[0m"

echo -e "\n\033[1m── Demo: Comunicação Inter-Broker ──────────────────────\033[0m\n"

python3 -c "
import os, sys, time, threading
sys.path.insert(0, '.')
from common.protocol import MSG_TEXT
from client.session import Session

received = []
lock = threading.Lock()

sender   = Session('inter-alice', strategy='round_robin')
receiver = Session('inter-bob',   strategy='round_robin')

sender.connect()
receiver.connect()

b_alice = sender.broker_info.get('broker_id')
b_bob   = receiver.broker_info.get('broker_id')
print(f'  · inter-alice → {b_alice}')
print(f'  · inter-bob   → {b_bob}')

sender.join('C')
receiver.join('C')

def on_msg(msg):
    with lock:
        received.append(msg.get('data', ''))

receiver.subscribe('C', MSG_TEXT, on_msg)
time.sleep(0.5)

msg_texto = 'Mensagem cross-broker de inter-alice para inter-bob'
sender.publish(MSG_TEXT, msg_texto)
print(f'  · inter-alice enviou: \"{msg_texto}\"')
time.sleep(2.0)

with lock:
    got = list(received)

if b_alice != b_bob:
    if msg_texto in got:
        print(f'  \033[32m✓\033[0m inter-bob recebeu a mensagem (inter-broker)')
    else:
        print(f'  \033[31m✗\033[0m inter-bob NÃO recebeu (msgs={got})')
else:
    print(f'  \033[33m~\033[0m Ambos no mesmo broker — tente novamente para distribuição diferente')
    if msg_texto in got:
        print(f'  \033[32m✓\033[0m inter-bob recebeu (mesmo broker)')

sender.disconnect()
receiver.disconnect()
"
echo -e "\n\033[1m── Fim da demonstração Inter-Broker ────────────────────\033[0m\n"
