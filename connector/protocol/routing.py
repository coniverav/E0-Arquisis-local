#Selecciona el endpoint interno de master según el tipo de mensaje.
def select_master_url(
    payload: dict,
    default_url: str,
    error_url: str,
) -> str:

    if payload.get("type") == "error":
        return error_url

    return default_url