def raw_item(aid, price, subject="Listing"):
    return {
        "urn": f"urn:subito:item:list:{aid}",
        "subject": subject,
        "urls": {"default": f"https://example.test/listing-{aid}.htm"},
        "features": {"/price": {"values": [{"key": str(price)}]}},
    }
