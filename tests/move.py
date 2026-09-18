"""Тест движения головы (аналог AiBot/tests/move.cpp).

MOVE:left/right/up/down:<deg> (+/-180) и MOVE:center через протокол;
ответы ACK:MOVE:* подтверждают выполнение команд сервоприводами.
"""

import time
from common import MOVE_TIMEOUT


async def run(suite) -> None:
    # Поворот влево/вправо/вверх/вниз.
    for axis, deg in (("left", 45), ("right", 45),
                      ("up", 30), ("down", 30)):
        await suite.send(f"MOVE:{axis}:{deg}")
        ack = await suite.expect(f"ACK:MOVE:{axis}:{deg}", timeout=MOVE_TIMEOUT)
        suite.report(ack is not None, f"move {axis} {deg}", extra=ack or "-")
        time.sleep(2)

    # Абсолютные позиции +/-90.
    for axis, deg in (("left", 90), ("right", 180), ("left", 90)):
        await suite.send(f"MOVE:{axis}:{deg}")
        ack = await suite.expect(f"ACK:MOVE:{axis}:{deg}", timeout=MOVE_TIMEOUT)
        suite.report(ack is not None, f"move {axis} {deg} (pan +/-180)",
                     extra=ack or "-")
        time.sleep(2)

    # Возврат в центр.
    await suite.send("MOVE:center")
    ack = await suite.expect("ACK:MOVE:center", timeout=MOVE_TIMEOUT)
    suite.report(ack is not None, "move center", extra=ack or "-")