# Videoconferência Distribuída — ZeroMQ

Sistema de videoconferência distribuída em Python 3 com ZeroMQ, suportando texto, áudio e vídeo em tempo real com cluster de brokers e failover automático.

## Arquitetura

```
[Clientes CLI — rodam nativamente]
        │  REQ/REP descoberta
        ▼
[Registry :5550]          ← service discovery, presença global
        │  heartbeat
        ▼
[Broker-1 :5555-5559] ←PUB/SUB→ [Broker-2 :5565-5569] ←PUB/SUB→ [Broker-3 :5575-5579]
```

- **Registry**: service discovery e agregação de presença
- **Brokers**: cluster com forwarding inter-broker via PUB/SUB mesh
- **Clientes**: rodam **fora do Docker**, conectam-se pela rede local

Documentação detalhada: [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md)

---

## Pré-requisitos

**Python (clientes e modo local)**:
```bash
# macOS — portaudio para PyAudio (áudio)
brew install portaudio

pip install -r requirements.txt
```

**Docker (opcional — modo infra containerizada)**:
- Docker Desktop instalado e rodando
- `docker compose` disponível

---

## Modo 1 — Infraestrutura via Docker (recomendado)

Registry e brokers rodam em containers; clientes rodam nativamente na mesma máquina ou em outras máquinas da rede local.

### 1. Exportar o IP da máquina host

Os clientes precisam alcançar os brokers pelo IP real da máquina onde o Docker está rodando.

```bash
# macOS
export HOST_IP=$(ipconfig getifaddr en0)

# Linux
export HOST_IP=$(ip route get 1 | awk '{print $7; exit}')
```

> Se os clientes rodarem **na mesma máquina** que o Docker, `HOST_IP=127.0.0.1` funciona (padrão já configurado).

### 2. Subir a infra

```bash
docker compose up --build registry broker-1 broker-2 broker-3
```

### 3. Iniciar clientes (terminais separados, fora do Docker)

```bash
# Script auxiliar — cria venv, instala deps e inicia:
./run_client.sh --id alice --room A
./run_client.sh --id bob   --room A

# Ou diretamente:
REGISTRY_HOST=${HOST_IP:-127.0.0.1} REGISTRY_PORT=5550 \
python -m client.client --id alice --room A
```

---

## Modo 2 — Tudo local, sem Docker

Os testes automatizados e as demos sobem registry e brokers como subprocessos Python — nenhum container necessário.

```bash
# Suite de testes (12 cenários):
python3 demo/test_phase0.py

# Demos individuais:
python3 demo/demo_failover.py       # failover automático
python3 demo/demo_inter_broker.py   # comunicação entre brokers
python3 demo/demo_multi_grupo.py    # 3 salas em paralelo
```

Para iniciar clientes manualmente no modo local:

```bash
# Sobe registry + brokers locais em segundo plano (portas padrão 5550-5579)
python -m registry.registry &
BROKER_ID=b1 BROKER_BASE_PORT=5555 BROKER_ADVERTISE_HOST=127.0.0.1 python -m broker.broker &
BROKER_ID=b2 BROKER_BASE_PORT=5565 BROKER_ADVERTISE_HOST=127.0.0.1 python -m broker.broker &

# Inicia cliente
REGISTRY_HOST=127.0.0.1 REGISTRY_PORT=5550 python -m client.client --id alice --room A
```

### Comandos disponíveis no cliente

| Comando | Descrição |
|---------|-----------|
| `/join <sala>` | Entra em uma sala (A–K) |
| `/leave` | Sai da sala atual |
| `/rooms` | Lista todas as salas ativas |
| `/who` | Membros da sala atual |
| `/activatecamera` | Liga/desliga câmera (toggle) |
| `/mic` | Liga/desliga microfone (toggle) |
| `/help` | Exibe ajuda |
| `/quit` | Encerra o cliente |

---

## Testes automatizados

Valida o sistema sem Docker (sobe processos Python localmente):

```bash
python3 demo/test_phase0.py
```

Testa 5 cenários:
1. Service discovery
2. Chat de texto (mesmo broker)
3. Presença (`/who`, `/rooms`)
4. Comunicação inter-broker
5. Failover automático

---

## Demonstrações

Há duas versões de cada demo: **local** (sem Docker, sobe tudo sozinha) e **Docker** (usa a infra já rodando).

### Com Docker (recomendado)

Requer `docker compose up` rodando antes.

```bash
# Inter-broker: alice e bob em brokers distintos se comunicam
python3 demo/demo_inter_broker_docker.py

# Multi-grupo: salas A, B e C em paralelo, todos inter-broker
python3 demo/demo_multi_grupo_docker.py

# Failover: derruba container, cliente reconecta automaticamente, restaura
python3 demo/demo_failover_docker.py
```

### Sem Docker (standalone)

Cada script sobe seu próprio registry e brokers como subprocessos Python.

```bash
python3 demo/demo_inter_broker.py
python3 demo/demo_multi_grupo.py
python3 demo/demo_failover.py
```

---

## QoS por tipo de mídia

| Tipo | Comportamento |
|------|---------------|
| **Texto** | Retry até 3× com backoff (garantia de entrega) |
| **Áudio** | Drop se fila cheia + número de sequência (detecta perdas) |
| **Vídeo** | Drop se fila cheia + qualidade adaptativa (20–50%) |

---

## Failover

Quando um broker cai:
1. O cliente detecta N heartbeats consecutivos sem ACK
2. Chama `reconnect()`: fecha sockets, consulta Registry por novo broker
3. Reabre sockets e envia `JOIN` para restaurar a sala
4. Restaura todas as subscriptions ativas

---

## Salas disponíveis

**A, B, C, D, E, F, G, H, I, J, K** (configurável via `ROOMS` em `common/channels.py`)
