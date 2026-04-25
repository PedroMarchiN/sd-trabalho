#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# demo/demo_failover.sh
# Demonstra failover automático: derruba broker, cliente reconecta sozinho.
#
# Pré-requisito: infraestrutura Docker rodando
#   docker compose up --build registry broker-1 broker-2 broker-3
#
# Uso:
#   bash demo/demo_failover.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")/.."

PASS="\033[32m✓\033[0m"
INFO="\033[36m·\033[0m"

echo -e "\n\033[1m── Demo: Failover Automático ───────────────────────────\033[0m\n"

# ── 1. Sobe cliente e entra em sala D ─────────────────────────────────────────
echo -e "  ${INFO} Conectando cliente 'demo-user' à sala D via round-robin…"
REGISTRY_HOST=localhost REGISTRY_PORT=5550 \
HEARTBEAT_INTERVAL=2.0 HEARTBEAT_TIMEOUT=8.0 HEARTBEAT_RETRIES=3 \
python3 -c "
import os, sys, time
sys.path.insert(0, '.')
from client.session import Session

s = Session('demo-user')
s.connect()
broker = s.broker_info.get('broker_id')
print(f'  · Conectado ao broker: {broker}')
s.join('D')
print(f'  · Entrou na sala D')
time.sleep(2.0)

print(f'\n  · Derrubando {broker} via docker compose stop ...')
import subprocess
subprocess.run(['docker', 'compose', 'stop', broker], check=False)

print('  · Aguardando failover (até 12s)...')
start = time.time()
while time.time() - start < 12:
    time.sleep(1)
    if s.broker_info and s.broker_info.get('broker_id') != broker and s._connected:
        break

new_broker = s.broker_info.get('broker_id') if s.broker_info else 'nenhum'
room_ok = s.current_room == 'D'

print()
if new_broker != broker and s._connected:
    print(f'  \033[32m✓\033[0m Failover bem-sucedido → {new_broker}')
else:
    print(f'  \033[31m✗\033[0m Failover falhou (broker={new_broker}, connected={s._connected})')

if room_ok:
    print(f'  \033[32m✓\033[0m Sala D restaurada automaticamente')
else:
    print(f'  \033[31m✗\033[0m Sala não restaurada (atual: {s.current_room!r})')

s.disconnect()

# Reinicia o broker derrubado para não deixar estado inconsistente
subprocess.run(['docker', 'compose', 'start', broker], check=False)
print(f'\n  · {broker} reiniciado.')
"
echo -e "\n\033[1m── Fim da demonstração de Failover ─────────────────────\033[0m\n"
