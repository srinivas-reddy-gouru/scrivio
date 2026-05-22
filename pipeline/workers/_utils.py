def extract_response_text(response) -> str:
    content = getattr(response, "content", response)

    if isinstance(content, str):
        return content

    text_parts = []
    for item in content:
        if isinstance(item, str):
            text_parts.append(item)
        elif isinstance(item, dict) and "text" in item:
            text_parts.append(item["text"])
        elif hasattr(item, "text"):
            text_parts.append(item.text)

    return "".join(text_parts)
