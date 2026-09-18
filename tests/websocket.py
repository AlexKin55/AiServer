"""Тест WebSocket-обмена (аналог AiBot/tests/websocket.cpp): send/recv.

PING -> PONG:<uptime> — подтверждает двусторонний обмен командами и
ответами по WebSocket. Состояние online/offline определяется самим фактом
установленного соединения, поэтому отдельный heartbeat/STATUS не используется.
"""


async def run(suite) -> None:
    await suite.send("PING")
    pong = await suite.expect("PONG:")
    suite.report(pong is not None, "PING -> PONG", extra=pong or "-")