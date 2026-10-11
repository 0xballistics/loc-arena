FROM python:3.12-slim

RUN pip install --no-cache-dir "pydantic>=2.13.5,<3"

WORKDIR /srv

COPY notes_service.py access_requests.py ./

USER nobody

EXPOSE 8000

CMD ["python", "-m", "notes_service"]
