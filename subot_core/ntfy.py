from email.header import Header

from curl_cffi import requests


def fmt_price(p):
    return str(int(p)) if p == int(p) else str(p)


def encode_header(value):
    value = str(value).replace("\r", " ").replace("\n", " ")
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        # Email header folding inserts a newline and is invalid in an HTTP
        # header value.  The continuation's leading space remains to separate
        # adjacent encoded words.
        return Header(value, "utf-8").encode().replace("\r", "").replace("\n", "")
    return value


def notify(cfg, item):
    location = " ".join(
        x
        for x in [
            item["town"],
            f"({item['city']})" if item["city"] else "",
        ]
        if x
    )
    body = "\n".join(
        x
        for x in [
            item["subject"],
            f"{fmt_price(item['price'])} \u20ac",
            location,
            item["url"],
        ]
        if x
    )

    endpoint = f"{cfg['ntfy_server'].rstrip('/')}/{cfg['ntfy_topic']}"
    headers = {"Title": encode_header(item["subject"]), "Click": item["url"]}
    if cfg.get("ntfy_token"):
        headers["Authorization"] = f"Bearer {cfg['ntfy_token']}"

    r = requests.post(endpoint, data=body.encode("utf-8"), headers=headers, timeout=20)
    r.raise_for_status()
