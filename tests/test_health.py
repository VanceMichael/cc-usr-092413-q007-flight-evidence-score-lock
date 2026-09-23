from fastapi.testclient import TestClient
from app import app
def test_health(): assert TestClient(app).get('/healthz').json() == {'status':'ok'}
