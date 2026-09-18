"""Тест камеры/видео (аналог AiBot/tests/video.cpp).

В протоколе взаимодействия пока нет команды CAMERA:*, поэтому тест
пропускается (SKIP). Добавится вместе с командой трансляции видео.
"""


async def run(suite) -> None:
    print("[TEST] SKIP: video/camera - нет команды CAMERA:* в протоколе")