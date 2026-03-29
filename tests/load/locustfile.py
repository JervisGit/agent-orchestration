"""Load testing for AO Platform API using Locust.

Run: locust -f tests/load/locustfile.py --host http://localhost:8000
"""

from locust import HttpUser, between, task


class AOWorkflowUser(HttpUser):
    """Simulates a DSAI app calling the AO Platform API."""

    wait_time = between(0.5, 2.0)

    @task(3)
    def health_check(self):
        self.client.get("/health")

    @task(5)
    def list_workflows(self):
        self.client.get("/api/workflows/")

    @task(2)
    def run_workflow(self):
        self.client.post(
            "/api/workflows/email-flow/run",
            json={
                "input": "I need help with my account",
                "identity": {"mode": "service", "tenant_id": "test"},
            },
        )

    @task(1)
    def list_pending_hitl(self):
        self.client.get("/api/hitl/pending")

    @task(2)
    def list_policies(self):
        self.client.get("/api/policies/")

    @task(1)
    def create_and_delete_policy(self):
        resp = self.client.post(
            "/api/policies/",
            json={
                "name": f"load-test-policy",
                "stage": "pre_execution",
                "action": "log",
            },
        )
        if resp.status_code == 200:
            policy_id = resp.json().get("id", "unknown")
            self.client.delete(f"/api/policies/{policy_id}")
