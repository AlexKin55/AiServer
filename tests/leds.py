"""Тест светодиодов (аналог AiBot/tests/leds.cpp).

LED:<r>,<g>,<b> для разных цветов и off; ответ ACK:LED:* подтверждает
установку подсветки.
"""
COLORS = [
    ("Red", 255, 0, 0),
    ("Green", 0, 255, 0),
    ("Blue", 0, 0, 255),
    ("Yellow", 255, 255, 0),
    ("Cyan", 0, 255, 255),
    ("Magenta", 255, 0, 255),
    ("White", 255, 255, 255),
]


async def run(suite) -> None:
    for name, r, g, b in COLORS:
        await suite.send(f"LED:{r},{g},{b}")
        ack = await suite.expect(f"ACK:LED:{r},{g},{b}")
        suite.report(ack is not None, f"led {name}", extra=ack or "-")

    await suite.send("LED:0,0,0")
    ack = await suite.expect("ACK:LED:0,0,0")
    suite.report(ack is not None, "led off", extra=ack or "-")