import asyncio
import io
import logging
import os
import shutil
import sys
import unicodedata
import uuid
from concurrent.futures import ProcessPoolExecutor
from typing import List

import aiofiles as aiof
import uvicorn
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

try:
    from libmat2 import parser_factory, UNSUPPORTED_EXTENSIONS
    from libmat2 import check_dependencies, UnknownMemberPolicy
except ValueError as ex:
    print(ex)
    sys.exit(1)

app = FastAPI(
    title="MetaCleaner",
    description="""Removes metadata from file using the mat2 library. 
The following formats are supported: avi, bmp, css, epub/ncx, flac, gif, jpeg,
m4a/mp2/mp3/…, mp4, odc/odf/odg/odi/odp/ods/odt/…, off/opus/oga/spx/…, pdf,
png, ppm, pptx/xlsx/docx/…, svg/svgz/…, tar/tar.gz/tar.bz2/tar.xz/…, tiff,
torrent, wav, wmv, zip, …""",
    summary="File metadata cleaner.",
    version="0.0.1",
    docs_url=None,  # disable to allow overriding of cdn
    redoc_url=None,  # disable
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# do not re-create the pool with every request, only create it once
pool = ProcessPoolExecutor()


@app.get("/", include_in_schema=False)
@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=app.title + " - Swagger UI",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_js_url="/static/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui.css",
    )


@app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
async def swagger_ui_redirect():
    return get_swagger_ui_oauth2_redirect_html()


@app.get("/redoc", include_in_schema=False)
async def redoc_html():
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title=app.title + " - ReDoc",
        redoc_js_url="/static/redoc.standalone.js",
    )


@app.post("/show/",
          tags=["Metadata"],
          response_class=JSONResponse,
          responses={
              200: {
                  "content": {
                      "application/json": {
                          "example":
                              {"mod-date": 1704067200,
                               "format": "PDF-1.4",
                               "title": "test title",
                               "creation-date": 1704067200,
                               "creator": "creator"}
                      }
                  },
                  "description": "Return the file metadata.",
              }
          }, )
async def show(file: UploadFile = File(...), sandbox: bool = True):
    """
    Extracts and return the file metadata
    """
    filename = f'{uuid.uuid4()}{file.filename}'
    async with aiof.open(filename, "wb") as out:
        await out.write(await file.read())
        await out.flush()
    try:
        print(os.path.abspath(filename))
        metadata = await get_meta(os.path.abspath(filename), sandbox)
        return JSONResponse(metadata,
                            media_type='text/plain')
    except HTTPException as e:  # pass through http exceptions
        raise e
    except Exception as e:  # any other exceptions
        __print_without_chars("[-] something went wrong when processing %s: %s" % (filename, e))
        raise HTTPException(status_code=500, detail="Metadata cannot be extracted")
    finally:
        os.remove(filename)


async def get_meta(filename: str, sandbox: bool):
    if not __check_file(filename):
        raise HTTPException(status_code=500, detail="File error")

    try:
        p, mtype = parser_factory.get_parser(filename)  # type: ignore
    except ValueError as e:
        __print_without_chars("[-] something went wrong when processing %s: %s" % (filename, e))
        raise HTTPException(status_code=400, detail="something went wrong during processing: {}".format(e))
    if p is None:
        __print_without_chars("[-] %s's format (%s) is not supported" % (filename, mtype))
        raise HTTPException(status_code=400, detail="format ({}) is not supported".format(mtype))
    p.sandbox = sandbox
    return p.get_meta()


def __check_file(filename: str, mode: int = os.R_OK) -> bool:
    if not os.path.exists(filename):
        __print_without_chars("[-] %s doesn't exist." % filename)
        return False
    elif not os.path.isfile(filename):
        __print_without_chars("[-] %s is not a regular file." % filename)
        return False
    elif not os.access(filename, mode):
        mode_str: List[str] = list()
        if mode & os.R_OK:
            mode_str += 'readable'
        if mode & os.W_OK:
            mode_str += 'writeable'
        __print_without_chars("[-] %s is not %s." % (filename, 'nor '.join(mode_str)))
        return False
    return True


@app.post("/clean/",
          tags=["Metadata"],
          response_class=StreamingResponse,
          responses={
              200: {
                  "content": {"application/octet-stream": {
                      "example": "(binary content of file with metadata removed)"
                  }},
                  "description": "Return the file with metadata removed.",
              }
          }, )
async def clean(file: UploadFile = File(...),
                lightweight: bool = False,
                unknown_members: UnknownMemberPolicy = UnknownMemberPolicy.ABORT,
                sandbox: bool = True):
    """
    Remove metadata from file and return the cleaned file
    """
    filename = f'{uuid.uuid4()}{file.filename}'
    async with aiof.open(filename, "wb") as out:
        await out.write(await file.read())
        await out.flush()
    prefix, extension = os.path.splitext(file.filename)

    policy = UnknownMemberPolicy(unknown_members)
    # if policy == UnknownMemberPolicy.KEEP:
    #     logging.warning('Keeping unknown member files may leak metadata in the resulting file!')

    loop = asyncio.get_event_loop()

    try:
        is_success = await loop.run_in_executor(pool, clean_meta, os.path.abspath(filename), lightweight, True, sandbox,
                                                policy)
        if is_success:
            data = await cache_delete_file(filename)
            if data is not None:
                return StreamingResponse(data,
                                         media_type='application/octet-stream',
                                         headers={
                                             'Content-Disposition': 'attachment; filename="{}"'.format(
                                                 prefix + '_metaclean' + extension)})
    except (ValueError, RuntimeError) as e:  # handle exceptions from clean_meta
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException as e:
        raise e
    finally:
        if os.path.isfile(os.path.abspath(filename)):
            os.remove(os.path.abspath(filename))

    raise HTTPException(status_code=400, detail="Failed to remove metadata")


def clean_meta(filename: str, is_lightweight: bool, inplace: bool, sandbox: bool,
               policy: UnknownMemberPolicy) -> bool:
    mode = (os.R_OK | os.W_OK) if inplace else os.R_OK
    if not __check_file(filename, mode):
        return False

    try:
        p, mtype = parser_factory.get_parser(filename)  # type: ignore
    except ValueError as e:
        __print_without_chars("[-] something went wrong when cleaning %s: %s" % (filename, e))
        raise ValueError("something went wrong during cleaning: {}".format(e))
    if p is None:
        __print_without_chars("[-] %s's format (%s) is not supported" % (filename, mtype))
        raise ValueError("format ({}) is not supported".format(mtype))
    p.unknown_member_policy = policy
    p.lightweight_cleaning = is_lightweight
    p.sandbox = sandbox

    try:
        logging.debug('Cleaning %s…', filename)
        ret = p.remove_all()
        if ret is True:
            shutil.copymode(filename, p.output_filename)
            if inplace is True:
                os.rename(p.output_filename, filename)
        return ret
    except RuntimeError as e:
        __print_without_chars("[-] %s can't be cleaned: %s" % (filename, e))
        raise RuntimeError("can't be cleaned: {}".format(e))


async def cache_delete_file(filename):
    cached_file = io.BytesIO()
    async with aiof.open(os.path.abspath(filename), 'rb') as fo:
        cached_file.write(await fo.read())
    cached_file.seek(0)
    os.remove(os.path.abspath(filename))
    return cached_file


# @app.get("/health", tags=["Health"])
@app.get("/healthz", tags=["Health"])
def get_health():
    # __print_without_chars("Dependencies for mat2 %s:" % )
    for key, value in sorted(check_dependencies().items()):
        __print_without_chars(
            '- %s: %s %s' % (key, 'yes' if value['found'] else 'no', '(optional)' if not value['required'] else ''))
    return "OK"


def __print_without_chars(s: str):
    """ Remove control characters
    We might use 'Cc' instead of 'C', but better safe than sorry
    https://www.unicode.org/reports/tr44/#GC_Values_Table
    """
    print(''.join(ch for ch in s if not unicodedata.category(ch).startswith('C')))


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=5000, log_level="info")
