"""
common/protocol.py
Envelope padrão de mensagens usando msgpack.
Todos os serviços importam daqui.
"""
import time
import uuid
import msgpack


# ── Tipos de mensagem ──────────────────────────────────────────────────────────
MSG_TEXT     = "text"
MSG_AUDIO    = "audio"
MSG_VIDEO    = "video"
MSG_PRESENCE = "presence"
MSG_CONTROL  = "control"   # join / leave / heartbeat / ack

# ── Tipos de controle ──────────────────────────────────────────────────────────
CTRL_JOIN      = "join"
CTRL_LEAVE     = "leave"
CTRL_HEARTBEAT = "heartbeat"
CTRL_ACK       = "ack"
CTRL_BROKER_HB = "broker_heartbeat"   # inter-broker


def encode(msg_type: str, sender_id: str, room: str, payload, extra: dict = None) -> bytes:
    """
    Serializa uma mensagem no envelope padrão.

    Parâmetros
    ----------
    msg_type  : MSG_TEXT | MSG_AUDIO | MSG_VIDEO | MSG_PRESENCE | MSG_CONTROL
    sender_id : ID único do remetente (cliente ou broker)
    room      : sala de destino  (ex: "salaA")
    payload   : bytes (mídia) ou str (texto/controle)
    extra     : dict opcional com campos adicionais
    """
    envelope = {
        "v":    1,
        "id":   str(uuid.uuid4()),
        "type": msg_type,
        "from": sender_id,
        "room": room,
        "ts":   time.time(),
        "data": payload,
    }
    if extra:
        envelope.update(extra)
    return msgpack.packb(envelope, use_bin_type=True)


def decode(raw: bytes) -> dict:
    """Desserializa um envelope msgpack."""
    return msgpack.unpackb(raw, raw=False)


def topic(room: str, msg_type: str) -> bytes:
    """
    Gera o tópico ZMQ para filtragem PUB/SUB.
    Exemplo: topic("salaA", "video") → b"salaA.video"
    """
    return f"{room}.{msg_type}".encode()


def encode_with_topic(msg_type: str, sender_id: str, room: str, payload, extra: dict = None) -> list[bytes]:
    """
    Retorna [tópico, payload_serializado] — formato multipart ZMQ.
    Pronto para sock.send_multipart([...]).
    """
    t = topic(room, msg_type)
    p = encode(msg_type, sender_id, room, payload, extra)
    return [t, p]