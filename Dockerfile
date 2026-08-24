FROM python:3.13-slim

WORKDIR /app

COPY . /app

RUN python -m pip install --upgrade pip && \
    python -m pip install .

# run.py binds loopback by default so a workstation does not expose an
# unauthenticated server on every interface. A container has to bind all of
# them or published ports never reach it, and the isolation boundary is the
# container rather than the bind address.
ENV PLEXORA_HOST=0.0.0.0

# Outside the image, data lives in a per-user platform directory. In here that
# would be inside the container's ephemeral filesystem, so it is an explicit
# path under /app that a volume can be mounted over.
ENV PLEXORA_DATA_PATH=/app/data

# Switches the import page to container-appropriate path hints -- a host path
# typed into this UI has to be one the container can see. Previously carried by
# run.py's second positional argument, which the CMD below never passed.
ENV PLEXORA_DOCKER=1

CMD ["python", "run.py"]
