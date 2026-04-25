"""
client/media.py
─────────────────────────────────────────────────────────────────────────────
Captura e renderização de câmera, vídeo e áudio.

CameraCapture  — captura webcam, chama on_frame(jpeg_bytes) a FPS frames/s
VideoWindow    — exibe janelas opencv por peer
AudioCapture   — captura microfone, chama on_chunk(bytes) a cada chunk PCM
AudioPlayer    — reproduz chunks PCM recebidos de peers

Notas de implementação
──────────────────────
  • opencv usa imshow() que requer DISPLAY.
  • QT_QPA_PLATFORM=xcb é forçado para evitar crash.
  • PyAudio é opcional: se não instalado, AudioCapture/Player indicam indisponível.
"""

import os
import sys
import time
import threading
import logging
import queue
import cv2
import numpy as np

# ── PyAudio (opcional) ────────────────────────────────────────────────────────
try:
    import pyaudio as _pa
    _AUDIO = True
except ImportError:
    _pa    = None
    _AUDIO = False

# Parâmetros de áudio (voz)
_AUDIO_RATE     = 16_000
_AUDIO_CHANNELS = 1
_AUDIO_CHUNK    = 1024   # ~64 ms por chunk a 16 kHz

log = logging.getLogger("client.media")


# ══════════════════════════════════════════════════════════════════════════════
class AudioCapture:
    """
    Captura áudio do microfone e chama on_chunk(bytes) para cada frame PCM.
    QoS: publica sem garantia — descarta se o callback demorar.
    """

    def __init__(self, on_chunk):
        self._cb      = on_chunk
        self._running = False
        self._pa      = None
        self._stream  = None

    def start(self) -> tuple:
        if not _AUDIO:
            return False, "PyAudio não instalado"
        try:
            self._pa = _pa.PyAudio()
            self._stream = self._pa.open(
                format=_pa.paInt16,
                channels=_AUDIO_CHANNELS,
                rate=_AUDIO_RATE,
                input=True,
                frames_per_buffer=_AUDIO_CHUNK,
            )
            self._running = True
            threading.Thread(target=self._loop, daemon=True, name="mic").start()
            return True, ""
        except Exception as e:
            return False, str(e)

    def stop(self) -> None:
        self._running = False
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
        self._stream = None
        self._pa     = None

    def _loop(self) -> None:
        while self._running:
            try:
                data = self._stream.read(_AUDIO_CHUNK, exception_on_overflow=False)
                try:
                    self._cb(data)
                except Exception:
                    pass
            except Exception:
                break


# ══════════════════════════════════════════════════════════════════════════════
class AudioPlayer:
    """
    Reproduz chunks PCM recebidos de peers.
    Usa fila pequena (maxsize=10); descarta o mais antigo se cheia — QoS áudio.
    """

    def __init__(self):
        self._q       = queue.Queue(maxsize=10)
        self._running = False
        self._pa      = None
        self._stream  = None

    def start(self) -> bool:
        if not _AUDIO:
            return False
        try:
            self._pa = _pa.PyAudio()
            self._stream = self._pa.open(
                format=_pa.paInt16,
                channels=_AUDIO_CHANNELS,
                rate=_AUDIO_RATE,
                output=True,
                frames_per_buffer=_AUDIO_CHUNK,
            )
            self._running = True
            threading.Thread(target=self._loop, daemon=True, name="speaker").start()
            return True
        except Exception as e:
            log.warning("AudioPlayer não pôde iniciar: %s", e)
            return False

    def stop(self) -> None:
        self._running = False
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
        self._stream = None
        self._pa     = None

    def push(self, chunk: bytes) -> None:
        """Enfileira chunk; descarta o mais antigo se a fila estiver cheia."""
        if self._q.full():
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
        try:
            self._q.put_nowait(chunk)
        except queue.Full:
            pass

    def _loop(self) -> None:
        while self._running:
            try:
                chunk = self._q.get(timeout=0.1)
                self._stream.write(chunk)
            except queue.Empty:
                pass
            except Exception:
                break

# ── Configura env ────────────────────────────────────────────────────────────
os.environ.setdefault("QT_LOGGING_RULES", "*.debug=false;qt.qpa.*=false")
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
os.environ.setdefault("XDG_SESSION_TYPE", "x11")

_GUI = bool(os.environ.get("DISPLAY", "")) or sys.platform == "darwin"
_CV2 = True

_print_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
class CameraCapture:
    FPS     = 10
    QUALITY = 50
    WIDTH   = 320
    HEIGHT  = 240

    def __init__(self, on_frame):
        self._cb = on_frame
        self._running = False

    def start(self) -> tuple[bool, str]:
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="cam").start()
        return True, ""

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        # backend mais estável no Linux
        cap = cv2.VideoCapture(0, cv2.CAP_V4L2)

        if not cap.isOpened():
            self._running = False
            log.warning("câmera não encontrada em /dev/video0")
            return

        interval = 1.0 / self.FPS
        last = 0.0

        while self._running:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue

            now = time.time()
            if now - last < interval:
                time.sleep(0.005)
                continue
            last = now

            frame = cv2.resize(frame, (self.WIDTH, self.HEIGHT))
            ok, buf = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.QUALITY])

            if ok:
                try:
                    self._cb(buf.tobytes())
                except Exception:
                    pass

        cap.release()


# ══════════════════════════════════════════════════════════════════════════════
class VideoWindow:
    def __init__(self):
        self._q = queue.Queue(maxsize=3)
        self._windows: set = set()
        self._running = _CV2

        # controle de remoção (thread-safe)
        self._to_remove = {}  # peer_id -> timestamp

        if self._running:
            threading.Thread(target=self._loop, daemon=True, name="vidwin").start()

    def push(self, peer_id: str, jpeg_bytes: bytes) -> None:
        if not _CV2:
            return

        if self._q.full():
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass

        try:
            self._q.put_nowait((peer_id, jpeg_bytes))
        except queue.Full:
            pass

    def remove(self, peer_id: str) -> None:
        """
        Marca janela para fechamento seguro (na thread de render).
        """
        if not _CV2 or not _GUI:
            return

        try:
            # mostra frame preto antes de fechar
            black = np.zeros((240, 320, 3), dtype=np.uint8)
            cv2.putText(black, "Camera desligada", (50, 110),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (100, 100, 100), 2)

            ok, buf = cv2.imencode(".jpg", black)
            if ok:
                self.push(peer_id, buf.tobytes())
        except Exception:
            pass

        # agenda remoção (NÃO fecha aqui)
        self._to_remove[peer_id] = time.time()

    def close(self) -> None:
        self._running = False
        if _CV2 and _GUI:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

    def _loop(self) -> None:
    # silencia warnings do Qt 
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            old_stderr = os.dup(2)
            os.dup2(devnull, 2)
            os.close(devnull)
        except Exception:
            old_stderr = None

        last_log = {}
        frame_cnt = {}

        try:
            while self._running:
                now = time.time()

                # ── Processa remoções (fechamento de janelas) ───────────────
                for peer_id in list(self._to_remove.keys()):
                    if now - self._to_remove[peer_id] >= 2.0:
                        try:
                            cv2.destroyWindow(f"Camera [{peer_id}]")
                        except Exception:
                            pass
                        self._windows.discard(peer_id)
                        del self._to_remove[peer_id]

                # ── Recebe frame da fila ────────────────────────────────────
                try:
                    peer_id, data = self._q.get(timeout=0.5)
                except queue.Empty:
                    # mantém GUI responsiva mesmo sem frames
                    if _GUI and self._windows:
                        try:
                            cv2.waitKey(1)
                        except Exception:
                            pass
                    continue

                frame_cnt[peer_id] = frame_cnt.get(peer_id, 0) + 1

                # ── Renderização ────────────────────────────────────────────
                if _GUI:
                    try:
                        buf = np.frombuffer(data, dtype=np.uint8)
                        frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)

                        if frame is not None:
                            cv2.imshow(f"Camera [{peer_id}]", frame)
                            self._windows.add(peer_id)

                        cv2.waitKey(1)

                    except Exception as e:
                        log.debug("vidwin: %s", e)

                else:
                    now = time.time()
                    if now - last_log.get(peer_id, 0) >= 3.0:
                        last_log[peer_id] = now
                        with _print_lock:
                            sys.stdout.write(
                                f"\r  📷 Vídeo de {peer_id}: "
                                f"{frame_cnt[peer_id]} frames\n> ")
                        sys.stdout.flush()
        finally:
            try:
                if old_stderr is not None:
                    os.dup2(old_stderr, 2)
                    os.close(old_stderr)
            except Exception:
                pass