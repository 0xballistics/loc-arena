# The notes image: the service plus the IAM client, which the stack passes as the named build context
# `meridian-iam` (company/meridian-iam). Only the client and contracts are used: stdlib alone.
FROM python:3.12-slim

# pip 26.2.2 is the minimum that stops trusting the compromised emSign CAs; the wheel's hash keeps it safe.
RUN pip install --no-cache-dir "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70" && pip install --no-cache-dir "pydantic>=2.13.5,<3"

WORKDIR /srv

COPY notes_service.py iam-manifest.json ./
COPY --from=meridian-iam /meridian_iam ./meridian_iam

USER nobody

EXPOSE 8000

CMD ["python", "-m", "notes_service"]
