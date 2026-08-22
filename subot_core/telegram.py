"""Telegram Bot API notification delivery."""

from curl_cffi import requests

from .ntfy import fmt_price


def notify(cfg, item):
    """Send one listing to the configured Telegram chat."""

    location = " ".join(
        x
        for x in [
            item["town"],
            f"({item['city']})" if item["city"] else "",
        ]
        if x
    )
    text = "\n".join(
        x
        for x in [
            item["subject"],
            f"{fmt_price(item['price'])} €",
            location,
            item["url"],
        ]
        if x
    )
    endpoint = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
    response = requests.post(
        endpoint,
        json={"chat_id": cfg["telegram_chat_id"], "text": text},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError("Telegram rejected the notification request")
