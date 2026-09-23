from fastapi import FastAPI

app = FastAPI(title="翼装竞速证据服务")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/v1/flights")
def flights():
    return {"items": []}
