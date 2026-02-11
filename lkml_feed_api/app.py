"""FastAPI app and routes."""

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from .models import ApiResponse
from .sdk import LKMLFeedClient

logging.basicConfig(level=logging.INFO)

# 通过环境变量配置，逗号分隔
# LKML_SUBSYSTEMS=linux-doc
# LKML_KEYWORDS=zh_cn
_subsystems = [s.strip() for s in os.getenv("LKML_SUBSYSTEMS", "linux-doc").split(",") if s.strip()]
_keywords_env = os.getenv("LKML_KEYWORDS", "zh_cn")
_keywords = [k.strip() for k in _keywords_env.split(",") if k.strip()] or None
_body_concurrency = int(os.getenv("LKML_BODY_CONCURRENCY", "1"))

client = LKMLFeedClient(
    _subsystems, keywords=_keywords, body_concurrency=_body_concurrency
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    client.close()


app = FastAPI(title="lkml-feed-service", lifespan=lifespan)


@app.get("/")
def ping() -> ApiResponse:
    return ApiResponse(code=200, message="pong")


@app.get("/latest")
def latest() -> ApiResponse:
    result = client.get_latest()
    return ApiResponse(
        data={"entries": [e.model_dump() for e in result.entries]}
    )


@app.post("/rewind")
def rewind(n: int) -> ApiResponse:
    client.rewind(n)
    return ApiResponse(message=f"rewound {n} messages")


@app.post("/reset")
def reset() -> ApiResponse:
    client.reset()
    return ApiResponse(message="reset to latest")


def run() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    run()
