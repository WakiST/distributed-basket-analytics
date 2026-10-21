"""Ограниченные JSON Lines поверх TCP, без внешних зависимостей."""

import json
import socket
import threading

MAX_FRAME = 2 * 1024 * 1024


class ProtocolError(ValueError):
    pass


def encode(message):
    try:
        payload = json.dumps(message, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise ProtocolError(str(exc)) from exc
    if len(payload) > MAX_FRAME:
        raise ProtocolError("Сообщение превышает 2 МиБ")
    return payload


class Connection:
    def __init__(self, sock):
        self.sock = sock
        self.sock.settimeout(1.0)
        self.buffer = bytearray()
        self.write_lock = threading.Lock()

    def send(self, message):
        payload = encode(message)
        with self.write_lock:
            self.sock.sendall(payload)

    def receive(self, stop=None, deadline=None):
        import time
        while stop is None or not stop.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Истекло время ожидания сообщения")
            position = self.buffer.find(b"\n")
            if position >= 0:
                if position + 1 > MAX_FRAME:
                    raise ProtocolError("Сообщение превышает 2 МиБ")
                line = bytes(self.buffer[:position])
                del self.buffer[:position + 1]
                try:
                    message = json.loads(line.decode("utf-8"),
                                         parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise ProtocolError("Некорректный JSON") from exc
                if not isinstance(message, dict):
                    raise ProtocolError("Ожидается JSON-объект")
                return message
            if len(self.buffer) >= MAX_FRAME:
                raise ProtocolError("Сообщение превышает 2 МиБ")
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise EOFError("Соединение закрыто")
            self.buffer.extend(chunk)
        raise EOFError("Остановка")

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def connect(host, port, timeout=5):
    return Connection(socket.create_connection((host, port), timeout=timeout))
