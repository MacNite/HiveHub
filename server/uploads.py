"""Body parsing for the large-upload endpoints, done only after authentication.

FastAPI reads and parses a route's declared body (File / Form / a Pydantic
model) BEFORE it runs any dependency, auth included. For a JSON body that means
holding the whole thing in memory; for multipart it means spooling every part
to a temp file. On the upload routes, which MaxBodySizeMiddleware allows to be
far larger than a normal request, anyone could make the server do that work
without a credential and only get the 401 afterwards.

So those routes declare only ``request: Request`` (plus their auth
dependencies, which then run first) and parse the body themselves with these
helpers. The middleware still caps the total bytes per path as a backstop.
"""

from typing import Optional, Type, TypeVar

from fastapi import HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, TypeAdapter, ValidationError
from starlette.datastructures import FormData
from starlette.datastructures import UploadFile as StarletteUploadFile

M = TypeVar("M", bound=BaseModel)

_BOOL = TypeAdapter(bool)


def _missing(field: str) -> RequestValidationError:
    return RequestValidationError(
        [{"type": "missing", "loc": ("body", field), "msg": "Field required", "input": None}]
    )


async def read_upload_form(request: Request, max_fields: int = 8) -> FormData:
    """Parse a multipart form carrying a single file and a few small fields."""
    try:
        return await request.form(max_files=1, max_fields=max_fields)
    except HTTPException:
        raise
    except Exception as exc:  # malformed multipart, wrong content type
        raise HTTPException(status_code=400, detail=f"Invalid form upload: {exc}") from exc


def form_file(form: FormData, field: str = "file") -> UploadFile:
    value = form.get(field)
    if not isinstance(value, StarletteUploadFile):
        raise _missing(field)
    return value


def form_str(form: FormData, field: str, default: Optional[str] = None) -> str:
    value = form.get(field)
    if value is None:
        if default is None:
            raise _missing(field)
        return default
    if not isinstance(value, str):
        raise RequestValidationError(
            [{"type": "string_type", "loc": ("body", field), "msg": "Input should be a valid string",
              "input": None}]
        )
    return value


def form_bool(form: FormData, field: str, default: bool) -> bool:
    """A boolean form field, coerced the way FastAPI's ``Form(bool)`` does."""
    value = form.get(field)
    if value is None:
        return default
    try:
        return _BOOL.validate_python(value)
    except ValidationError as exc:
        raise RequestValidationError(
            [{**e, "loc": ("body", field)} for e in exc.errors()]
        ) from exc


async def read_upload_bytes(file: UploadFile, max_bytes: int, what: str) -> bytes:
    """Read an uploaded file into memory, refusing it as soon as it passes max_bytes."""
    chunks = []
    size = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"{what} exceeds the {max_bytes // (1024 * 1024)} MB upload limit",
                )
            chunks.append(chunk)
    finally:
        await file.close()
    return b"".join(chunks)


async def parse_json_body(request: Request, model: Type[M]) -> M:
    """Validate a JSON body into ``model``, answering 422 like a declared body would."""
    body = await request.body()
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise RequestValidationError(
            [{**e, "loc": ("body", *e.get("loc", ()))} for e in exc.errors(include_url=False)],
            body=body,
        ) from exc


def multipart_openapi(fields: dict, required: list) -> dict:
    """``openapi_extra`` documenting a multipart body the route parses itself.

    ``fields`` maps a field name to "binary" (a file), "boolean" or "string"."""
    props = {
        name: {"type": "string", "format": "binary"} if kind == "binary" else {"type": kind}
        for name, kind in fields.items()
    }
    return {"requestBody": {"required": True, "content": {"multipart/form-data": {
        "schema": {"type": "object", "properties": props, "required": required}}}}}
