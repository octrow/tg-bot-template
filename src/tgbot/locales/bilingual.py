"""Тексты сразу на двух языках (спека localization): их читает
человек, который, возможно, не понимает текущий язык бота. Одни и те же
значения вливаются в оба каталога - так набор ключей локалей совпадает, а
двух копий, которые могли бы разойтись, нет."""

_TAIL = (
    "You can change it yourself any time: /language (also in the menu). "
    "We won't ask again for {days} days.\n"
    "Сменить язык можно и самому в любой момент: /language (есть в меню). "
    "Повторно спросим не раньше чем через {days} дней."
)

STRINGS: dict = {
    # Ключ - язык, на который предлагается переключиться.
    "LANGUAGE_OFFER": {
        "en": (
            "You're writing in English - switch the bot to English?\n"
            "Вы пишете по-английски - переключить бота на английский?\n\n" + _TAIL
        ),
        "ru": (
            "Вы пишете по-русски - переключить бота на русский?\n"
            "You're writing in Russian - switch the bot to Russian?\n\n" + _TAIL
        ),
    },
    # Ключ - язык, на который переключает кнопка.
    "LANGUAGE_OFFER_SWITCH": {
        "en": "Switch to English / На английский",
        "ru": "На русский / Switch to Russian",
    },
    # Ключ - текущий язык, который кнопка оставляет.
    "LANGUAGE_OFFER_KEEP": {
        "en": "Keep English / Оставить английский",
        "ru": "Оставить русский / Keep Russian",
    },
}
