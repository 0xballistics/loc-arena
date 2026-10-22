# The notes image: the service plus the IAM client, which the stack passes as the named build context
# `meridian-iam` (company/meridian-iam). Only the client and contracts are used: stdlib alone.
FROM python:3.12-slim

RUN pip install --no-cache-dir "pydantic>=2.13.5,<3"

WORKDIR /srv

COPY notes_service.py iam-manifest.json ./
COPY --from=meridian-iam /meridian_iam ./meridian_iam

USER nobody

EXPOSE 8000

CMD ["python", "-m", "notes_service"]
