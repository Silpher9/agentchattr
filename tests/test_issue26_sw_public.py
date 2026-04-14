import sys
from pathlib import Path
import unittest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
import run

class TestIssue26ServiceWorkerPublic(unittest.TestCase):
    def setUp(self):
        # Configure app with a mock config and session token
        self.session_token = "test-session-token"
        self.cfg = {
            "server": {"port": 8300, "host": "127.0.0.1", "data_dir": "./data"},
            "agents": {},
            "images": {"upload_dir": "./uploads"}
        }
        # Reset FastAPI state for testing
        app.app = app.FastAPI()
        app.configure(self.cfg, session_token=self.session_token)
        
        # We need to add the /sw.js route which is normally added in run.py
        # But we want to test the middleware in app.py.
        @app.app.get("/sw.js")
        async def service_worker():
            return {"ok": True}
            
        @app.app.get("/api/protected")
        async def protected():
            return {"ok": True}

        self.client = TestClient(app.app)

    def test_sw_is_public(self):
        # GET /sw.js without token should return 200 (currently fails with 403)
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200, "Service worker should be public")

    def test_protected_route_remains_protected(self):
        # GET /api/protected without token should return 403
        response = self.client.get("/api/protected")
        self.assertEqual(response.status_code, 403, "API routes should remain protected")

    def test_protected_route_with_token_works(self):
        # GET /api/protected with token should return 200
        response = self.client.get("/api/protected", headers={"x-session-token": self.session_token})
        self.assertEqual(response.status_code, 200, "Protected route should work with valid token")

if __name__ == "__main__":
    unittest.main()
