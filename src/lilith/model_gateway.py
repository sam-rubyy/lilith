import json
import os
import urllib.error
import urllib.request
from typing import Protocol


Message = dict[str, str]


class ModelGateway(Protocol):
    @property
    def model_name(self) -> str:
        ...

    def chat(self, messages: list[Message]) -> str:
        ...


class OllamaGateway:
    def __init__(
        self,
        model: str,
        base_url: str | None = None,
    ):
        self._model = model
        self._base_url = (base_url or os.environ.get("LILITH_OLLAMA_URL", "http://localhost:11434")).rstrip("/")

    @property
    def model_name(self) -> str:
        return self._model

    def _request(self, payload: dict) -> dict:
        payload.setdefault("keep_alive", os.environ.get("LILITH_KEEP_ALIVE", "0"))
        if "qwen3" in self._model.lower():
            payload.setdefault("think", False)
        request = urllib.request.Request(
            url=f"{self._base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=300,
            ) as response:
                body = response.read(1048577)
                if len(body) > 1048576:
                    raise RuntimeError("Model response exceeded the 1 MiB limit")
                return json.loads(body.decode("utf-8"))

        except urllib.error.HTTPError as error:
            error.close()
            raise RuntimeError(
                f"Ollama returned HTTP "
                f"{error.code}"
            ) from error

        except urllib.error.URLError as error:
            raise RuntimeError(
                f"Could not connect to Ollama at "
                f"{self._base_url}: "
                f"{error.reason}"
            ) from error

    def chat(
        self,
        messages: list[Message],
    ) -> str:
        data = self._request(
            {
                "model": self._model,
                "messages": messages,
                "stream": False,
            }
        )

        try:
            return data["message"]["content"]

        except (KeyError, TypeError) as error:
            raise RuntimeError(
                "Unexpected Ollama response structure"
            ) from error

    def chat_stream(self, messages: list[Message]):
        """Yield visible content as NDJSON arrives; keep reasoning fields internal."""
        payload = {"model": self._model, "messages": messages, "stream": True,
                   "keep_alive": os.environ.get("LILITH_KEEP_ALIVE", "0")}
        if "qwen3" in self._model.lower():
            payload["think"] = False
        request = urllib.request.Request(f"{self._base_url}/api/chat", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        total = 0
        with urllib.request.urlopen(request, timeout=300) as response:
            while True:
                line = response.readline(65537)
                if not line:
                    raise RuntimeError("Reply stream ended before completion")
                total += len(line)
                if len(line) > 65536 or total > 4 * 1048576:
                    raise RuntimeError("Reply stream exceeded its byte budget")
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("error"):
                    raise RuntimeError(str(event["error"]))
                content = event.get("message", {}).get("content", "")
                if not isinstance(content, str):
                    raise RuntimeError("Invalid streamed content")
                if content:
                    yield content
                if event.get("done") is True:
                    return

    def chat_json(
        self,
        messages: list[Message],
        schema: dict | None = None,
    ) -> dict:
        data = self._request(
            {
                "model": self._model,
                "messages": messages,
                "stream": False,
                "format": schema or "json",
                "options": {"num_predict": 4096, "temperature": 0},
            }
        )

        try:
            content = data["message"]["content"]
            result = json.loads(content)
            if not isinstance(result, dict):
                raise TypeError("Expected a JSON object")
            return result

        except (
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ) as error:
            raise RuntimeError(
                "Model returned an invalid JSON object"
            ) from error
