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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.protocol import MSG_TEXT
from common.channels  import ROOMS
from client.session   import Session, SessionError

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
        self._running     = False

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
            "/join":  lambda: self._cmd_join(arg),
            "/leave": lambda: self._cmd_leave(),
            "/rooms": lambda: self._cmd_rooms(),
            "/who":   lambda: self._cmd_who(),
            "/help":  lambda: self._cmd_help(),
            "/quit":  lambda: self._cmd_quit(),
            "/exit":  lambda: self._cmd_quit(),
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
            old = self.session.current_room
            self.session.unsubscribe(old, MSG_TEXT)

        ok = self.session.join(room)
        if not ok:
            print_error(f"Não foi possível entrar na sala {room}")
            return

        # Subscreve texto da nova sala
        self.session.subscribe(room, MSG_TEXT, self._on_text_message)
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
        data = self.session.list_rooms()
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
        data    = self.session.list_rooms()
        members = data.get("rooms", {}).get(room, [])
        with _print_lock:
            print(c(C.CYAN, f"\n── Membros de {room} ({len(members)}) ──"))
            for m in members:
                marker = c(C.GREEN, "● ") if m == self.client_id else "  "
                print(f"  {marker}{m}")
            print()

    def _cmd_help(self) -> None:
        with _print_lock:
            print(c(C.CYAN, """
── Comandos ───────────────────────────────────
  /join <sala>   Entra em uma sala (A–K)
  /leave         Sai da sala atual
  /rooms         Lista todas as salas ativas
  /who           Membros da sala atual
  /help          Este menu
  /quit          Encerra o cliente
────────────────────────────────────────────────
  Qualquer outro texto é enviado como mensagem.
────────────────────────────────────────────────
"""))

    def _cmd_quit(self) -> None:
        print_system("Encerrando…", C.DIM)
        self._running = False
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

    # ── Callback de recepção ───────────────────────────────────────────────────
    def _on_text_message(self, msg: dict) -> None:
        # Ignora mensagens do próprio cliente (eco)
        if msg.get("from") == self.client_id:
            return
        print_msg(msg)

    # ── Banner ─────────────────────────────────────────────────────────────────
    def _banner(self) -> None:
        print(c(C.CYAN + C.BOLD, """
╔═══════════════════════════════════════╗
║     VideoConf — Cliente de Texto      ║
║     ZeroMQ · Cluster Distribuído      ║
╚═══════════════════════════════════════╝"""))


# ── Entrypoint ─────────────────────────────────────────────────────────────────
def main() -> None:
    import uuid
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