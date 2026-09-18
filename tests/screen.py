"""Тест вывода эмоций на экран (аналог AiBot/tests/screen.cpp).

EMOTION:<name> для всех эмоций; ответ ACK:EMOTION:<name> подтверждает
установку выражения лица аватара.
"""
import time

EMOTIONS = ("neutral", "happy", "angry", "sad", "doubt", "sleepy")

async def run(suite) -> None:
    for name in EMOTIONS:
        await suite.send(f"EMOTION:{name}")
        ack = await suite.expect(f"ACK:EMOTION:{name}")
        suite.report(ack is not None, f"emotion {name}", extra=ack or "-")
        time.sleep(2)