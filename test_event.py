import json
import urllib.request
import uuid

event = {
    "event_id": str(uuid.uuid4()),
    "tool_name": "seo_tool",
    "component_name": "gpt-4",
    "service_type": "llm",
    "execution_type": "inference",
    "user_id": "user1",
    "org_id": "default",
    "input_data_size_mb": 0.2,
    "output_data_size_mb": 1.5,
    "tokens": {"input": 1200, "output": 300},
    "external_tools": [{"name": "serpapi", "cost": 0.01}],
    "latency_ms": 450,
}

data = json.dumps(event).encode()
req = urllib.request.Request("http://localhost:8000/telemetry/event", data=data, headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req)
print("Status:", resp.status)
print(json.dumps(json.loads(resp.read().decode()), indent=2))
