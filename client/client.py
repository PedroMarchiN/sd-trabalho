"""
client/client.py
─────────────────────────────────────────────────────────────────────────────
Cliente CLI de videoconferência — Fase 1 (texto + presença).

Funcionalidades
───────────────
  • Login com ID único
  • Descoberta automática de broker via Registry
  • Entrada e saída de salas (A–K)
  • Chat de texto em tempo real
  • Lista de membros da sala
  • Reconexão automática em caso de falha de broker

Comandos disponíveis
─────────────────────
  /join <sala>     — entra em uma sala (A–K)
  /leave           — sai da sala atual
  /rooms           — lista salas e membros
  /who             — membros da sala atual
  /help            — lista comandos
  /quit            — encerra o cliente

Uso
───
  python -m client.client --id alice
  python -m client.client --id bob --room B
  python -m client.client  # ID gerado automaticamente
"""

import os
import sys
import time
import argparse
import threading
import logging
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.protocol import MSG_TEXT, MSG_VIDEO
from common.channels  import ROOMS
from client.session   import Session, SessionError
from client.media     import CameraCapture, VideoWindow

# ── Prefixos de mensagens de sistema (câmera/mic) ─────────────────────────────
_SYS_PREFIX  = "__sys__:"
_CAM_ON_TAG  = "__cam_on__"
_CAM_OFF_TAG = "__cam_off__"

# ── Logging — só WARNING+ para não poluir a CLI ───────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("client.cli")


# ── Cores ANSI ────────────────────────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    CYAN   = "\033[36m"
    RED    = "\033[31m"
    BLUE   = "\033[34m"
    MAGENTA= "\033[35m"

USE_COLOR = sys.stdout.isatty()

def c(color: str, text: str) -> str:
    return f"{color}{text}{C.RESET}" if USE_COLOR else text


# ── Formatação de mensagens recebidas ─────────────────────────────────────────
_print_lock = threading.Lock()

def print_msg(msg: dict) -> None:
    """Imprime uma mensagem recebida sem bagunçar o prompt de input."""
    sender    = msg.get("from", "?")
    room      = msg.get("room", "?")
    payload   = msg.get("data", "")
    ts        = msg.get("ts", 0)
    time_str  = time.strftime("%H:%M:%S", time.localtime(ts))

    line = (
        f"\r{c(C.DIM, time_str)} "
        f"{c(C.CYAN, '[' + room + ']')} "
        f"{c(C.BOLD + C.GREEN, sender)}"
        f"{c(C.DIM, ':')} "
        f"{payload}"
    )
    with _print_lock:
        print(line)
        print("> ", end="", flush=True)   # restaura o prompt

def print_system(msg: str, color: str = C.YELLOW) -> None:
    with _print_lock:
        print(f"\r{c(color, '⚙ ' + msg)}")
        print("> ", end="", flush=True)

def print_error(msg: str) -> None:
    print_system(msg, C.RED)


# ══════════════════════════════════════════════════════════════════════════════
class ChatClient:
    """
    Cliente de chat interativo baseado em Session.
    """

    def __init__(self, client_id: str, initial_room: str = ""):
        self.client_id    = client_id
        self.initial_room = initial_room.upper() if initial_room else ""
        self.session      = Session(client_id)

        # Câmera
        self._cam_on  = False
        self._cam     = CameraCapture(self._on_local_frame)
        self._vidwin  = VideoWindow()
        self._running = False

    # ── Entrypoint ─────────────────────────────────────────────────────────────
    def run(self) -> None:
        self._banner()

        # Conecta ao broker
        print(c(C.DIM, "Conectando ao broker…"))
        try:
            self.session.connect()
        except SessionError as e:
            print(c(C.RED, f"Erro ao conectar: {e}"))
            sys.exit(1)

        print(c(C.GREEN, f"✓ Conectado como {c(C.BOLD, self.client_id)}"))
        print(c(C.DIM, "Digite /help para ver os comandos disponíveis.\n"))

        # Entra na sala inicial se especificada
        if self.initial_room:
            self._cmd_join(self.initial_room)

        self._running = True
        self._input_loop()

    # ── Loop de input ──────────────────────────────────────────────────────────
    def _input_loop(self) -> None:
        while self._running:
            try:
                print("> ", end="", flush=True)
                line = input("").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                self._cmd_quit()
                break

            if not line:
                continue

            if line.startswith("/"):
                self._dispatch_command(line)
            else:
                self._send_text(line)

    def _dispatch_command(self, line: str) -> None:
        parts  = line.split(maxsplit=1)
        cmd    = parts[0].lower()
        arg    = parts[1].strip() if len(parts) > 1 else ""

        dispatch = {
            "/join":           lambda: self._cmd_join(arg),
            "/leave":          lambda: self._cmd_leave(),
            "/rooms":          lambda: self._cmd_rooms(),
            "/who":            lambda: self._cmd_who(),
            "/activatecamera": lambda: self._cmd_camera(),
            "/help":           lambda: self._cmd_help(),
            "/quit":           lambda: self._cmd_quit(),
            "/exit":           lambda: self._cmd_quit(),
        }

        fn = dispatch.get(cmd)
        if fn:
            fn()
        else:
            print_error(f"Comando desconhecido: {cmd}. Digite /help.")

    # ── Comandos ───────────────────────────────────────────────────────────────
    def _cmd_join(self, room: str) -> None:
        room = room.upper()
        if room not in ROOMS:
            print_error(f"Sala inválida: '{room}'. Use uma de: {', '.join(ROOMS)}")
            return

        # Cancela subscrições da sala anterior
        if self.session.current_room:
            old_room = self.session.current_room
            self.session.unsubscribe(old_room, MSG_TEXT)
            self.session.unsubscribe(old_room, MSG_VIDEO)

        ok = self.session.join(room)
        if not ok:
            print_error(f"Não foi possível entrar na sala {room}")
            return

        # Subscreve texto e vídeo da nova sala
        self.session.subscribe(room, MSG_TEXT,  self._on_text_message)
        self.session.subscribe(room, MSG_VIDEO, self._on_video_message)
        print_system(f"Entrou na sala {c(C.BOLD, room)}", C.GREEN)

    def _cmd_leave(self) -> None:
        room = self.session.current_room
        if not room:
            print_error("Você não está em nenhuma sala.")
            return
        self.session.unsubscribe(room, MSG_TEXT)
        self.session.leave(room)
        print_system(f"Saiu da sala {room}")

    def _cmd_rooms(self) -> None:
        # Consulta Registry diretamente — agrega salas de TODOS os brokers
        data  = self.session.list_rooms()
        rooms = data.get("rooms", {})
        if not rooms:
            print_system("Nenhuma sala ativa no momento.")
            return
        with _print_lock:
            print(c(C.CYAN, "\n── Salas ativas ──────────────────"))
            for room, members in sorted(rooms.items()):
                marker = c(C.BOLD + C.GREEN, "►") if room == self.session.current_room else " "
                print(f"  {marker} {c(C.BOLD, room)} ({len(members)} membro(s)): "
                      f"{', '.join(members)}")
            print(c(C.CYAN, "──────────────────────────────────\n"))

    def _cmd_who(self) -> None:
        room = self.session.current_room
        if not room:
            print_error("Você não está em nenhuma sala.")
            return
        # Consulta Registry diretamente — agrega membros de TODOS os brokers
        members = self.session.who(room)
        with _print_lock:
            print(c(C.CYAN, f"\n── Membros de {room} ({len(members)}) ──"))
            for m in members:
                marker = c(C.GREEN, "● ") if m == self.client_id else "  "
                print(f"  {marker}{m}")
            print()

    def _cmd_help(self) -> None:
        with _print_lock:
            print(c(C.CYAN, """
── Comandos ──────────────────────────────────────────
  /join <sala>       Entra em uma sala (A–K)
  /leave             Sai da sala atual
  /rooms             Lista todas as salas ativas
  /who               Membros da sala atual
  /activatecamera    Liga/desliga câmera (toggle)
  /help              Este menu
  /quit              Encerra o cliente
──────────────────────────────────────────────────────
  Qualquer outro texto é enviado como mensagem.
──────────────────────────────────────────────────────
"""))

    def _cmd_quit(self) -> None:
        print_system("Encerrando…", C.DIM)
        self._running = False
        if self._cam_on:
            self._cam.stop()
        self._vidwin.close()
        self.session.disconnect()

    # ── Envio de texto ─────────────────────────────────────────────────────────
    def _send_text(self, text: str) -> None:
        if not self.session.current_room:
            print_error("Entre em uma sala primeiro: /join A")
            return
        try:
            self.session.publish(MSG_TEXT, text)
        except Exception as e:
            print_error(f"Erro ao enviar: {e}")

    # ── Callbacks de recepção ──────────────────────────────────────────────────
    def _on_text_message(self, msg: dict) -> None:
        if msg.get("from") == self.client_id:
            return
        text = msg.get("data", "")
        if text.startswith(_SYS_PREFIX):
            body = text[len(_SYS_PREFIX):]
            if body.startswith(_CAM_OFF_TAG):
                # Peer desligou câmera — fecha janela
                self._vidwin.remove(msg.get("from", ""))
                print_system(body[len(_CAM_OFF_TAG):], C.MAGENTA)
            else:
                notice = body[len(_CAM_ON_TAG):] if body.startswith(_CAM_ON_TAG) else body
                print_system(notice, C.MAGENTA)
        else:
            print_msg(msg)

    def _on_video_message(self, msg: dict) -> None:
        if msg.get("from") == self.client_id:
            return
        data = msg.get("data")
        if data:
            self._vidwin.push(msg["from"], data)

    # ── Callbacks de captura local ──────────────────────────────────────────────
    def _on_local_frame(self, jpeg: bytes) -> None:
        if self._cam_on and self.session.current_room:
            try:
                self.session.publish(MSG_VIDEO, jpeg)
            except Exception:
                pass

    # ── Câmera ─────────────────────────────────────────────────────────────────
    def _cmd_camera(self) -> None:
        if not self.session.current_room:
            print_error("Entre em uma sala primeiro.")
            return
        self._cam_on = not self._cam_on
        if self._cam_on:
            ok, msg = self._cam.start()
            if not ok:
                self._cam_on = False
                print_error(f"Câmera indisponível: {msg}")
            else:
                print_system("Câmera LIGADA 📷", C.GREEN)
                self._send_sys(f"{_CAM_ON_TAG}{self.client_id} ativou a câmera 📷")
        else:
            self._cam.stop()
            print_system("Câmera DESLIGADA", C.YELLOW)
            self._send_sys(f"{_CAM_OFF_TAG}{self.client_id} desligou a câmera")

    def _send_sys(self, text: str) -> None:
        """Envia notificação de sistema para a sala."""
        try:
            self.session.publish(MSG_TEXT, f"{_SYS_PREFIX}{text}")
        except Exception:
            pass

    # ── Banner ─────────────────────────────────────────────────────────────────
    def _banner(self) -> None:
        print(c(C.CYAN + C.BOLD, """
╔═══════════════════════════════════════╗
║     VideoConf — Cliente de Texto      ║
║     ZeroMQ · Cluster Distribuído      ║
╚═══════════════════════════════════════╝"""))


# ── Entrypoint ─────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Cliente CLI de videoconferência")
    parser.add_argument(
        "--id", dest="client_id",
        default=f"user-{uuid.uuid4().hex[:6]}",
        help="ID único do cliente (padrão: gerado automaticamente)",
    )
    parser.add_argument(
        "--room", dest="room", default="",
        help="Sala para entrar ao iniciar (A–K)",
    )
    args = parser.parse_args()

    ChatClient(client_id=args.client_id, initial_room=args.room).run()


if __name__ == "__main__":
    main()