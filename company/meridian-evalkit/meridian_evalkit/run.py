"""The runner: an eval run as a job on the cluster, scoring the model under test through the inference API.

``python -m meridian_evalkit.run --evalkit <url> --run <run_id> --model <name>`` is the command the eval
service hands out with each run; ``<name>`` is the model under test as the inference API serves it. The job
binds itself to the run, reads the run and its eval's items, puts each prompt to the model, scores the answers
against the references with the harness, reports the metrics the eval declares and exits; its stdout and
stderr are the job's logs. It holds nothing but its own credentials, from the environment its node set:
``MERIDIAN_EVALKIT_TOKEN`` on the eval service, ``MERIDIAN_GATEWAY_URL`` and ``MERIDIAN_GATEWAY_TOKEN`` for
the inference API. Whatever it cannot do, bind, read, ask or report, ends it with a non-zero exit and the
reason on stderr, and a run it reported nothing on stays ``started``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from meridian_datapipe.tokenize import Tokenizer
from meridian_evalkit.errors import RunnerError
from meridian_evalkit.harness import EvalItem, Harness, Prediction

EVALKIT_TOKEN_VARIABLE = "MERIDIAN_EVALKIT_TOKEN"
GATEWAY_URL_VARIABLE = "MERIDIAN_GATEWAY_URL"
GATEWAY_TOKEN_VARIABLE = "MERIDIAN_GATEWAY_TOKEN"
CALL_TIMEOUT_SECONDS = 60.0  # one call of the eval service or of the inference API


def call(method: str, url: str, token: str, body: object = None) -> Any:  # noqa: ANN401 - JSON
    """The JSON reply of ``method`` on ``url``, sent with ``token``; a refusal raises ``RunnerError``."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data, headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=CALL_TIMEOUT_SECONDS) as reply:
            return json.loads(reply.read() or b"null")
    except urllib.error.HTTPError as error:
        why = error.read().decode(errors="replace")[:500]
        raise RunnerError(f"{method} {url} answered {error.code}: {why}", code="evalkit.runner") from None
    except urllib.error.URLError as error:
        raise RunnerError(f"{method} {url} failed: {error.reason}", code="evalkit.runner") from None


class GatewayModel:
    """The model under test, asked through the inference API: each item's prompt as one chat message."""

    def __init__(
        self,
        url: str,
        token: str,
        model: str,
        prompts: Mapping[str, str],
        tokenizer: Tokenizer,
    ) -> None:
        """Ask ``model`` at ``url`` with ``token``; ``prompts`` is each item's text, which an item lacks."""
        self._url, self._token, self._model = url, token, model
        self._prompts, self._tokenizer = prompts, tokenizer

    def predict(self, item: EvalItem) -> Prediction:
        """The model's answer to ``item``, as the tokens the harness scores against the reference."""
        ask = {"model": self._model, "messages": [{"role": "user", "content": self._prompts[item.item_id]}]}
        reply = call("POST", f"{self._url}/chat/completions", self._token, ask)
        try:
            answer = reply["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RunnerError(
                f"no answer to item {item.item_id!r}: {reply!r:.200}",
                code="evalkit.runner",
            ) from None
        return Prediction(item_id=item.item_id, tokens=tuple(self._tokenizer.encode(str(answer).strip())))


def run_eval(evalkit_url: str, run_id: str, model: str, environment: Mapping[str, str]) -> dict[str, Any]:
    """Bind to run ``run_id`` on the eval service at ``evalkit_url``, score ``model`` on it and report it."""
    try:
        token = environment[EVALKIT_TOKEN_VARIABLE]
        gateway_url, gateway_token = environment[GATEWAY_URL_VARIABLE], environment[GATEWAY_TOKEN_VARIABLE]
    except KeyError as missing:
        raise RunnerError(f"the job's environment holds no {missing}", code="evalkit.runner") from None
    run_url = f"{evalkit_url}/runs/{run_id}"
    call("POST", f"{run_url}/start", token)
    run = call("GET", run_url, token)
    found = call("GET", f"{evalkit_url}/evals/{run['eval_id']}", token)
    tokenizer = Tokenizer()
    items = [
        EvalItem(
            item_id=item["item_id"],
            prompt=tuple(tokenizer.encode(item["prompt"])),
            reference=tuple(tokenizer.encode(item["reference"])),
            category=item["category"],
        )
        for item in found["items"]
    ]
    prompts = {item["item_id"]: item["prompt"] for item in found["items"]}
    report = Harness().run(GatewayModel(gateway_url, gateway_token, model, prompts, tokenizer), items)
    results = {
        "metrics": {name: report.metrics[name] for name in found["metrics"]},
        "item_ids": [item.item_id for item in items],
    }
    return call("POST", f"{run_url}/results", token, results)


def main(argv: list[str] | None = None) -> int:
    """Run the command: 0 once the run is reported, 1 with the reason on stderr otherwise."""
    parser = argparse.ArgumentParser(
        prog="python -m meridian_evalkit.run",
        description=__doc__.split("\n\n")[0],
    )
    parser.add_argument("--evalkit", required=True, help="the eval service's URL")
    parser.add_argument("--run", required=True, help="the run to score, which named this job")
    parser.add_argument("--model", required=True, help="the model under test, as the inference API serves it")
    arguments = parser.parse_args(argv)
    try:
        reported = run_eval(arguments.evalkit, arguments.run, arguments.model, os.environ)
    except RunnerError as error:
        print(f"the run was not reported: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"run_id": reported["run_id"], "metrics": reported["metrics"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
