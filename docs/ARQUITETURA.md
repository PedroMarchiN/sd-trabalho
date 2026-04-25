# Arquitetura do Sistema de Videoconferência Distribuída

## Visão Geral

Sistema de videoconferência distribuída em Python 3 com ZeroMQ, composto por três camadas:

```
[Clientes CLI]  ←→  [Cluster de Brokers]  ←→  [Registry]
```

Os clientes rodam **nativamente** (fora do Docker). O Registry e os Brokers sobem via `docker compose`.

---

## Componentes

### Registry (`registry/registry.py`)

- **Padrão ZMQ**: REQ/REP (socket único, single-threaded)
- **Porta**: 5550
- **Responsabilidades**:
  - Registra brokers ao iniciar (`action: register`)
  - Responde heartbeats de brokers (mantém `ts` atualizado)
  - Service discovery para clientes (`action: get_broker`)
  - Agrega presença de salas de todos os brokers (`/who`, `/rooms`)
  - Remove brokers sem heartbeat após `HEARTBEAT_TIMEOUT`

**Estratégias de descoberta**:
- `round_robin`: distribui clientes uniformemente
- `least_load`: direciona para o broker com menos clientes ativos

---

### Broker (`broker/broker.py`)

Cada instância executa **5 threads** + threads de sub-sistemas:

| Thread | Função |
|--------|--------|
| `proxy` | Redistribui mensagens XSUB → XPUB + forwarding inter-broker |
| `control` | Loop ROUTER para join/leave/heartbeat de clientes |
| `evict` | Remove clientes sem heartbeat periodicamente |
| `cluster_recv` | Recebe mensagens de outros brokers (SUB) |
| `cluster_send` | Envia mensagens ao cluster (único acessador do PUB) |
| `cluster_sync` | Sincroniza lista de peers com Registry |
| `hb_publish` | Publica heartbeat próprio (cluster + Registry) |

**Portas por instância** (base `B`):

| Offset | Socket | Uso |
|--------|--------|-----|
| B+0 | XSUB | Recebe publicações dos clientes |
| B+1 | XPUB | Distribui para assinantes locais |
| B+2 | ROUTER | Comandos de controle (join/leave/heartbeat) |
| B+3 | PUB | Publica mensagens para outros brokers |
| B+4 | SUB | Recebe mensagens de outros brokers |

**Thread safety**: o socket `cluster_pub` (PUB, B+3) é acessado **exclusivamente** pela thread `cluster_send` via `queue.Queue(maxsize=200)`. As demais threads apenas colocam mensagens na fila.

**Anti-loop inter-broker**: cada mensagem transporta um campo `hops: [broker_id, ...]`. O broker descarta mensagens onde seu próprio ID já aparece.

**XSUB subscrito a tudo**: o XSUB faz `send(b"\x01")` ao iniciar, garantindo que receba todos os tópicos — necessário para forwarding inter-broker mesmo sem assinantes locais.

---

### Cluster (`broker/broker_cluster.py`)

Modelo **PUB/SUB mesh**: cada broker tem um PUB (envia) e um SUB (recebe). O SUB conecta ao PUB de cada peer descoberto no Registry.

```
broker-A PUB ──→ broker-B SUB
broker-A PUB ──→ broker-C SUB
broker-B PUB ──→ broker-A SUB
...
```

**Heartbeat de cluster**: publicado no tópico `__hb__`. Inclui mapa `{sala: [membros]}` para que brokers remotos conheçam a presença nos outros nós.

**Evicção de peers**: peers sem heartbeat por `HEARTBEAT_TIMEOUT × 5` são removidos.

---

### Cliente (`client/session.py`, `client/client.py`)

O cliente mantém **3 sockets** para o broker ativo:

| Socket | Padrão | Uso |
|--------|--------|-----|
| PUB | PUB → XSUB | Publica mensagens |
| SUB | XPUB → SUB | Recebe mensagens da sala |
| DEALER | DEALER → ROUTER | Comandos (join/leave/heartbeat) |

**Descoberta**: REQ → REP ao Registry, com probe de disponibilidade antes de confirmar.

**Failover**: a thread `hb_thread` detecta N heartbeats perdidos consecutivos e chama `reconnect()`, que fecha os sockets, descobre novo broker, reabre e restaura a sala.

---

## Fluxo de Mensagens

### Publicação (mesmo broker)

```
Cliente PUB
    │ send_multipart([topic, payload])
    ▼
Broker XSUB (frontend)
    │ proxy thread: recv → send
    ▼
Broker XPUB (backend)
    │ filtra por tópico (subscription)
    ▼
Cliente SUB
```

### Publicação (inter-broker)

```
Cliente PUB (broker-A)
    │
    ▼
Broker-A XSUB → proxy → XPUB → assinantes locais
                    │
                    │ cluster.forward()
                    ▼
               outbox Queue
                    │
               _thread_send
                    │
                    ▼
             Broker-A cluster PUB
                    │
                    ▼
             Broker-B cluster SUB
                    │
               _thread_receive → inbox Queue
                    │
               proxy thread → XPUB
                    │
                    ▼
             Cliente SUB (broker-B)
```

---

## Protocolos de Heartbeat

Há três níveis de heartbeat:

1. **Cliente → Broker** (via DEALER/ROUTER): a cada `HEARTBEAT_INTERVAL`. Atualiza timestamp de presença. Detecta falha de broker.
2. **Broker → Registry** (via REQ/REP): a cada `HEARTBEAT_INTERVAL`. Mantém broker ativo no Registry. Inclui mapa de salas.
3. **Broker → Cluster peers** (via PUB/SUB): a cada `HEARTBEAT_INTERVAL`. Inclui mapa de salas para presença remota.

---

## QoS por Tipo de Mídia

| Tipo | HWM Envio | Comportamento | Garantia |
|------|-----------|---------------|---------|
| Texto | 1000 | Retry até 3× com backoff | Alta |
| Áudio | padrão | Drop se fila cheia + seqno | Baixa (latência) |
| Vídeo | padrão | Drop se fila cheia + qualidade adaptativa | Melhor esforço |

**Vídeo adaptativo**: o callback de captura local monitora drops consecutivos. Após 3 drops consecutivos, reduz JPEG quality (mínimo 20). Após 30 envios bem-sucedidos, restaura (máximo 50).

**Áudio seqno**: cada chunk de áudio carrega um número de sequência crescente. O receptor detecta gaps e loga perdas (não há retransmissão — baixa latência tem prioridade).

---

## Variáveis de Ambiente

| Variável | Padrão | Descrição |
|----------|--------|-----------|
| `REGISTRY_HOST` | `registry` | Hostname do Registry |
| `REGISTRY_PORT` | `5550` | Porta do Registry |
| `BROKER_ID` | `broker-1` | Identificador único do broker |
| `BROKER_HOST` | `0.0.0.0` | Interface de bind do broker |
| `BROKER_ADVERTISE_HOST` | `${HOST_IP:-127.0.0.1}` | IP anunciado aos clientes |
| `BROKER_BASE_PORT` | `5555` | Porta base (5 consecutivas usadas) |
| `HEARTBEAT_INTERVAL` | `1.0` | Intervalo de heartbeat (segundos) |
| `HEARTBEAT_TIMEOUT` | `5.0` | Timeout para considerar morto (segundos) |
| `HEARTBEAT_RETRIES` | `3` | Heartbeats perdidos antes de failover |

---

## Estrutura de Diretórios

```
T1-SistDist/
├── broker/
│   ├── broker.py          # Broker principal (XSUB/XPUB/ROUTER + threads)
│   ├── broker_cluster.py  # Cluster inter-broker (PUB/SUB mesh)
│   ├── heartbeat.py       # Heartbeat do broker (cluster + registry)
│   └── Dockerfile
├── client/
│   ├── client.py          # CLI interativo
│   ├── session.py         # Sessão ZMQ (descoberta + failover + QoS)
│   ├── media.py           # CameraCapture, VideoWindow, AudioCapture, AudioPlayer
│   └── Dockerfile
├── common/
│   ├── protocol.py        # Envelope msgpack (encode/decode/topic)
│   └── channels.py        # Constantes, portas, HWM (lidos de env vars)
├── registry/
│   ├── registry.py        # Service discovery REQ/REP
│   └── Dockerfile
├── demo/
│   ├── test_phase0.py     # Suite de testes automatizados (sem Docker)
│   ├── demo_failover.sh   # Demo: failover automático
│   ├── demo_inter_broker.sh # Demo: comunicação entre brokers
│   └── demo_multi_grupo.sh  # Demo: múltiplos grupos simultâneos
├── docs/
│   └── ARQUITETURA.md     # Este documento
├── docker-compose.yml
├── requirements.txt
└── run_client.sh
```
