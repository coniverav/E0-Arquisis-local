## Prompt

```
class EventOut(BaseModel):
    # En la base de datos las columnas se llaman package_body, valid_until,
    # received_at (snake_case). Acá les damos alias para que la API las
    # muestre en camelCase, como pide el enunciado.
    id: int
    idpk: str
    type: str
    packageBody: dict[str, Any] = Field(alias="package_body")
    #validUntil: Optional[datetime] = Field(default=None, alias="valid_until")
    receivedAt: datetime = Field(alias="received_at")

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
```

cómo puedo modificar esto para que las palabras se vean con camelCase en el local? si cambio el alias falla con el error que te mostré

## Respuesta

Encontré el problema. Cuando pones alias="package_body" en un campo llamado packageBody, ese alias sirve para las dos cosas a la vez: tanto para leer desde la base de datos como para mostrarlo en el JSON de salida. Por eso lo que ves en el navegador es package_body (snake_case), no packageBody — el alias "gana" también en la salida, que es justo lo contrario de lo que quieres. Y si intentas invertirlo (poner el alias en camelCase), se rompe la lectura desde la base de datos, porque ahí ya no encuentra el atributo correcto.

La solución es separar los dos usos: un alias solo para la salida (serialization_alias), dejando el nombre del campo igual al de la base de datos para que la lectura funcione sin trucos.

Archivo visualizado, editó un archivo

Reemplaza tu schemas.py con esto (ya tiene validUntil de vuelta, sin comentar) y corre docker compose up --build master. Ahora sí debería salir packageBody, validUntil y receivedAt en el JSON de /history.