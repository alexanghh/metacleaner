FROM python:3.11-slim AS buildbase

RUN apt-get update && apt-get install -y --no-install-recommends --no-install-suggests \
        ca-certificates \
        gcc \
        cmake \
        libgirepository1.0-dev \
        ffmpeg \
        mat2 \
        gir1.2-gdkpixbuf-2.0 \
        gir1.2-poppler-0.18 \
        gir1.2-rsvg-2.0 \
        libimage-exiftool-perl \
        pkg-config \
        libcairo2 \
        libcairo2-dev \
        python3.11-venv \
    && python3 -m venv /venv \
    && rm -rf /var/lib/apt/lists/*


FROM buildbase AS pybuild

COPY requirements.txt /requirements.txt
RUN /venv/bin/pip install -r requirements.txt


FROM python:3.11-slim AS runbase

RUN apt-get update && apt-get install -y --no-install-recommends --no-install-suggests \
        mat2 \
        ffmpeg \
#        gir1.2-gdkpixbuf-2.0 \
#        gir1.2-poppler-0.18 \
#        gir1.2-rsvg-2.0 \
#        libimage-exiftool-perl \
#        libcairo2 \
    && rm -rf /var/lib/apt/lists/*


FROM runbase

COPY --from=pybuild /venv /venv
COPY ./src/ /app
WORKDIR /app
EXPOSE 8080
# Run the FastAPI application using uvicorn server
CMD ["/venv/bin/python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]