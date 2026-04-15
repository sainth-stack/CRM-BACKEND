from fastapi.testclient import TestClient
from main import app
import json

client = TestClient(app)

def test_signup_api():
    payload = {
        "email": "test_api_user@test.com",
        "password": "password123"
    }
    print(f"Sending POST to /auth/demo/signup with {payload}...")
    try:
        response = client.post("/auth/demo/signup", json=payload)
        print(f"Status Code: {response.status_code}")
        print(f"Response Body: {response.text}")
    except Exception as e:
        print(f"CRITICAL ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_signup_api()
