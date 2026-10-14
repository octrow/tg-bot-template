"""Однократный вход тестового пользователя на тестовый DC Telegram.

Печатает в stdout строку `E2E_SESSION=...` (вопросы - в stderr), поэтому
вывод можно сразу дописать в `.env.e2e`: последняя строка ключа побеждает.
Сам скрипт на диск ничего не пишет.

    uv run --extra e2e python tests_e2e/make_session.py --api-id 123 --api-hash abc --dc 2
    uv run --extra e2e python tests_e2e/make_session.py --api-id 123 --api-hash abc --prod

Тестовый DC: аккаунт регистрируется заранее в официальном клиенте в
тестовом режиме (`sign_up` Telethon 1.x не работает), номер `99966XYYYY`,
X - номер DC, код входа - X пять раз (если не принят - шесть).
`--prod`: запасной аккаунт обычного Telegram, код приходит в приложение.
Подробности - README.md.
"""

import argparse
import asyncio
import sys
import termios

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

# Тестовые DC. DC2 - из документации Telethon; DC1/DC3 - из FAQ Pyrogram,
# сверить с my.telegram.org -> "test configuration".
TEST_DC_IP = {1: "149.154.175.10", 2: "149.154.167.40", 3: "149.154.175.117"}
TEST_DC_PORT = 80


def ask_secret(prompt: str) -> str:
    """Скрытый ввод без `getpass`: тот на Python 3.14 падает на первом же байте
    кириллицы (UnicodeDecodeError), а облачный пароль бывает и русским. Строка
    читается из терминала целиком байтами и декодируется уже собранной."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    with open("/dev/tty", "rb", buffering=0) as tty:
        fd = tty.fileno()
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        try:
            raw = b""
            while not raw.endswith(b"\n"):
                chunk = tty.read(1)
                if not chunk:
                    break
                raw += chunk
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stderr.write("\n")
    return raw.decode("utf-8", errors="strict").rstrip("\r\n")


def ask(prompt: str) -> str:
    """Вопрос - в stderr: stdout отдан строке сессии, его можно перенаправить
    прямо в .env.e2e, и сессия не пройдёт через экран и чаты."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return input().strip()


# Куда Telegram отправил код (auth.SentCodeType*): без этого 'код не пришёл'
# не отличить от 'пришёл в чат Telegram на другом устройстве'.
SENT_CODE_WHERE = {
    "SentCodeTypeApp": "сообщением в чат 'Telegram' в приложении (на всех ваших устройствах)",
    "SentCodeTypeSms": "по SMS",
    "SentCodeTypeCall": "звонком",
    "SentCodeTypeFlashCall": "сбросом звонка (код - последние цифры номера)",
    "SentCodeTypeMissedCall": "пропущенным звонком (код - последние цифры номера)",
    "SentCodeTypeEmailCode": "на почту, привязанную ко входу",
    "SentCodeTypeFragmentSms": "SMS через Fragment",
    "SentCodeTypeFirebaseSms": "SMS (Firebase)",
}


def _where(sent) -> str:
    kind = type(sent.type).__name__
    text = SENT_CODE_WHERE.get(kind, kind)
    nxt = getattr(sent, "next_type", None)
    if nxt is not None:
        text += f"; по r - {SENT_CODE_WHERE.get(type(nxt).__name__.replace('CodeType', 'SentCodeType'), type(nxt).__name__)}"
    timeout = getattr(sent, "timeout", None)
    if timeout:
        text += f" (повтор не раньше чем через {timeout} с)"
    return text


QR_ATTEMPTS = 3  # QR живёт ~30 с; столько раз показываем новый
QR_WAIT_S = 60


def _print_qr(url: str) -> None:
    import qrcode

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    # В stderr, как и вопросы: stdout - только строка сессии. Ссылку не печатаем
    # текстом - по ней входят в аккаунт, ей нечего делать в логах и чатах.
    qr.print_ascii(out=sys.stderr, invert=True)


async def _qr_sign_in(client) -> None:
    """Вход без кода: Telegram на телефоне -> Настройки -> Устройства ->
    Подключить устройство -> сканировать. Код в чат 'Telegram' для сторонних
    приложений порой 'отправлен', но не доходит - QR этот путь обходит."""
    login = await client.qr_login()
    for attempt in range(QR_ATTEMPTS):
        sys.stderr.write(
            "\nTelegram на телефоне -> Настройки -> Устройства -> Подключить устройство,"
            " отсканируйте:\n"
        )
        _print_qr(login.url)
        try:
            await login.wait(timeout=QR_WAIT_S)
            return
        except TimeoutError:
            if attempt == QR_ATTEMPTS - 1:
                raise SystemExit("QR не отсканирован - запустите скрипт ещё раз")
            await login.recreate()


async def make(api_id: int, api_hash: str, dc: int | None, *, qr: bool = False) -> str:
    """`dc=None` - обычный Telegram: Telethon сам идёт на прод-DC."""
    session = StringSession()
    if dc is not None:
        session.set_dc(dc, TEST_DC_IP[dc], TEST_DC_PORT)
    client = TelegramClient(session, api_id, api_hash)
    await client.connect()
    try:
        if qr:
            try:
                await _qr_sign_in(client)
            except SessionPasswordNeededError:
                await client.sign_in(password=ask_secret("Облачный пароль (2FA): "))
            return client.session.save()
        hint = f"99966{dc}YYYY" if dc is not None else "в международном формате, +7..."
        phone = ask(f"Номер ({hint}): ")
        sent = await client.send_code_request(phone)
        default = str(dc) * 5 if dc is not None else ""
        while True:
            sys.stderr.write(f"Код отправлен: {_where(sent)}\n")
            code = ask(f"Код{f' (по умолчанию {default})' if default else ''}, r - отправить заново: ")
            if code.lower() != "r":
                code = code or default
                break
            # Повторный запрос с тем же номером Telethon превращает в
            # auth.resendCode - следующим способом (`next_type`), если он есть.
            sent = await client.send_code_request(phone)
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            await client.sign_in(password=ask_secret("Облачный пароль (2FA): "))
        return client.session.save()
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-id", type=int, required=True)
    parser.add_argument("--api-hash", required=True)
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--dc", type=int, choices=sorted(TEST_DC_IP), default=2,
                       help="тестовый DC (по умолчанию 2)")
    where.add_argument("--prod", action="store_true",
                       help="обычный Telegram (E2E_TELEGRAM_ENV=prod)")
    parser.add_argument("--qr", action="store_true",
                        help="вход по QR с телефона вместо кода (если код не приходит)")
    args = parser.parse_args()
    session = asyncio.run(
        make(args.api_id, args.api_hash, None if args.prod else args.dc, qr=args.qr)
    )
    print(f"E2E_SESSION={session}")


if __name__ == "__main__":
    main()
