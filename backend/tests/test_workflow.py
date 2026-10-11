def transition(client, task_id, command, **kwargs):
    return client.post(
        f"/api/tasks/{task_id}/transitions",
        json={"command": command, "actor": "test-operator", **kwargs},
    )


def triage(client, task_id, *, risk="LOW", complexity="MEDIUM", **kwargs):
    return transition(
        client,
        task_id,
        "COMPLETE_ANALYSIS",
        triage={"risk": risk, "complexity": complexity, **kwargs},
    )


def test_illegal_transition_is_rejected_and_audited(api, create_task):
    client, repository = api
    task = create_task()

    response = transition(client, task["id"], "START_WORK")
    assert response.status_code == 409
    assert client.get(f"/api/tasks/{task['id']}").json()["status"] == "BACKLOG"

    events_response = client.get(f"/api/tasks/{task['id']}/events")
    assert events_response.status_code == 200
    event = events_response.json()[0]
    assert event["command"] == "START_WORK"
    assert event["outcome"] == "REJECTED"
    assert event["from_status"] == "BACKLOG"
    assert event["to_status"] is None
    assert repository.list_events(task["id"])[0].outcome.value == "REJECTED"


def test_low_risk_ai_path_skips_human_approval(api, create_task):
    client, _ = api
    task = create_task(risk="LOW", complexity="MEDIUM", execution_mode="AI")
    assert task["approval_required"] is False

    assert transition(client, task["id"], "START_ANALYSIS").json()["status"] == "ANALYZING"
    assert triage(client, task["id"]).json()["status"] == "TRIAGED"
    ready = transition(client, task["id"], "APPLY_POLICY").json()
    assert ready["status"] == "READY"
    assert ready["approval_required"] is False
    assert transition(client, task["id"], "START_WORK").json()["status"] == "IN_PROGRESS"
    assert transition(client, task["id"], "SUBMIT_FOR_VALIDATION").json()["status"] == "VALIDATING"

    missing_test_evidence = transition(
        client,
        task["id"],
        "VALIDATION_PASSED",
        checks_passed=True,
    )
    assert missing_test_evidence.status_code == 409
    assert (
        transition(
            client,
            task["id"],
            "VALIDATION_PASSED",
            checks_passed=True,
            check_run_id="run-17",
        ).json()["status"]
        == "SECURITY_REVIEW"
    )
    assert (
        transition(
            client,
            task["id"],
            "SECURITY_PASSED",
            security_review_id="security-17",
        ).json()["status"]
        == "APPROVED"
    )
    assert (
        transition(
            client,
            task["id"],
            "CREATE_PR",
            pull_request_url="https://github.com/acme/nexus/pull/17",
        ).json()["status"]
        == "PR_CREATED"
    )


def test_critical_risk_routes_to_human_queue(api, create_task):
    client, _ = api
    task = create_task(risk="CRITICAL", complexity="HIGH", execution_mode="AI")
    assert task["execution_mode"] == "HUMAN"
    assert task["approval_required"] is True

    transition(client, task["id"], "START_ANALYSIS")
    assert (
        triage(client, task["id"], risk="CRITICAL", complexity="HIGH").json()["status"] == "TRIAGED"
    )
    assert transition(client, task["id"], "APPLY_POLICY").json()["status"] == "HUMAN_QUEUE"
    assert transition(client, task["id"], "ASSIGN_HUMAN").status_code == 409
    assigned = transition(
        client,
        task["id"],
        "ASSIGN_HUMAN",
        assigned_agents=["human-reviewer"],
    )
    assert assigned.status_code == 200
    assert assigned.json()["status"] == "READY"
    assert assigned.json()["assigned_agents"] == ["human-reviewer"]


def test_high_risk_ai_is_upgraded_to_hybrid_and_requires_approval(api, create_task):
    client, _ = api
    task = create_task(risk="HIGH", complexity="MEDIUM", execution_mode="AI")
    assert task["execution_mode"] == "HYBRID"
    assert task["approval_required"] is True

    transition(client, task["id"], "START_ANALYSIS")
    triaged = triage(client, task["id"], risk="HIGH", complexity="MEDIUM").json()
    assert triaged["execution_mode"] == "HYBRID"
    assert triaged["approval_required"] is True
    transition(client, task["id"], "APPLY_POLICY")
    transition(client, task["id"], "START_WORK")
    transition(client, task["id"], "SUBMIT_FOR_VALIDATION")
    transition(
        client,
        task["id"],
        "VALIDATION_PASSED",
        checks_passed=True,
        check_run_id="run-18",
    )
    assert (
        transition(
            client,
            task["id"],
            "SECURITY_PASSED",
            security_review_id="security-18",
        ).json()["status"]
        == "AWAITING_APPROVAL"
    )
    assert transition(client, task["id"], "APPROVE").json()["status"] == "APPROVED"


def test_policy_requires_human_for_unassessed_tasks(api, create_task):
    _, _ = api
    task = create_task()
    assert task["approval_required"] is True
