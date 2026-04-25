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

**Infraestrutura (Docker)**:
```bash
docker compose up --build registry broker-1 broker-2 broker-3
```

**Clientes (Python nativo)**:
```bash
# macOS: portaudio para PyAudio
brew install portaudio

pip install -r requirements.txt
```

---

## Subir a infraestrutura

```bash
# Detecta IP da máquina para que clientes externos possam conectar
export HOST_IP=$(ip route get 1 | awk '{print $7; exit}')   # Linux
# macOS:
export HOST_IP=$(ipconfig getifaddr en0)

docker compose up --build registry broker-1 broker-2 broker-3
```

---

## Iniciar clientes

```bash
# Usando o script auxiliar (cria venv, instala deps, inicia cliente):
./run_client.sh --id alice --room A
./run_client.sh --id bob   --room A

# Ou diretamente (com env vars):
REGISTRY_HOST=localhost REGISTRY_PORT=5550 \
HEARTBEAT_INTERVAL=2.0 HEARTBEAT_TIMEOUT=8.0 \
python -m client.client --id alice --room A
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

Todas as demos sobem seus próprios processos — **não precisam de Docker**.

```bash
# Failover: mata um broker, cliente reconecta automaticamente
python3 demo/demo_failover.py

# Inter-broker: clientes em brokers distintos se comunicam
python3 demo/demo_inter_broker.py

# Multi-grupo: salas A, B e C em paralelo, clientes distribuídos
python3 demo/demo_multi_grupo.py
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
