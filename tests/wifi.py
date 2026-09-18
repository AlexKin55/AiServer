"""Тест Wi-Fi (аналог AiBot/tests/wifi.cpp).

Проверяется неявно: раз робот установил WebSocket-соединение с нашим сервером,
значит Wi-Fi работает (подключение к сети + IP).
"""


async def run(suite) -> None:
    suite.report(True, "robot connected (wifi link)",
                 extra=suite.ws.remote_address[0])