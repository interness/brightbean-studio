"""Submitting drafts to the approval workflow over MCP and REST.

Before this, agents could only create drafts; moving one into Approvals
needed the composer's "Submit for Approval" button. Covers the
``submit_for_review`` tool, ``create_draft(submit_for_review=true)`` and
``POST /api/v1/posts/{id}/submit``.
"""

from __future__ import annotations

import json

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.approvals.models import ApprovalAction
from apps.composer.models import PlatformPost, Post
from apps.mcp.protocol import INVALID_PARAMS
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership

MCP_URL = "/api/v1/mcp/"


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


def _call(client, name, arguments):
    body = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": name, "arguments": arguments}, "id": 1}
    r = client.post(MCP_URL, data=json.dumps(body), content_type="application/json")
    return r.json()


def _inner(body):
    assert "error" not in body, body
    return json.loads(body["result"]["content"][0]["text"])


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="submit@example.com", password="testpass123", name="Submitter", tos_accepted_at=timezone.now()
    )


@pytest.fixture
def workspace(db, user):
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    org = Organization.objects.create(name="Submit Org")
    ws = Workspace.objects.create(name="Submit WS", organization=org, approval_workflow_mode="required_internal")
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=ws, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER)
    return ws


def _account(workspace, platform="linkedin_personal", suffix="1"):
    from apps.social_accounts.models import SocialAccount

    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-{suffix}",
        account_name=f"{platform} {suffix}",
        connection_status="connected",
    )


@pytest.fixture
def account(workspace):
    return _account(workspace)


@pytest.fixture
def instagram(workspace):
    return _account(workspace, platform="instagram", suffix="ig")


@pytest.fixture
def client(user, workspace, account, instagram):
    key = services.issue_api_key(
        workspace=workspace,
        social_accounts=[account, instagram],
        issued_by=user,
        name="submit",
        permissions=list(PERMISSION_KEYS),
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


def _draft(workspace, user, account, status="draft"):
    post = Post.objects.create(workspace=workspace, author=user, caption="needs review")
    PlatformPost.objects.create(post=post, social_account=account, status=status)
    return post


@pytest.mark.django_db
class TestSubmitForReviewTool:
    def test_moves_draft_to_pending_review(self, client, workspace, user, account):
        post = _draft(workspace, user, account)
        inner = _inner(_call(client, "submit_for_review", {"post_id": str(post.id)}))
        assert inner["platform_posts"][0]["status"] == "pending_review"
        assert post.platform_posts.get().status == "pending_review"
        assert ApprovalAction.objects.filter(post=post, action=ApprovalAction.ActionType.SUBMITTED).exists()

    def test_resubmits_after_changes_requested(self, client, workspace, user, account):
        post = _draft(workspace, user, account, status="changes_requested")
        inner = _inner(_call(client, "submit_for_review", {"post_id": str(post.id)}))
        assert inner["platform_posts"][0]["status"] == "pending_review"

    def test_refused_without_approval_workflow(self, client, workspace, user, account):
        workspace.approval_workflow_mode = "none"
        workspace.save(update_fields=["approval_workflow_mode"])
        post = _draft(workspace, user, account)
        body = _call(client, "submit_for_review", {"post_id": str(post.id)})
        assert body["error"]["code"] == INVALID_PARAMS
        assert "approval workflow" in body["error"]["message"]
        assert post.platform_posts.get().status == "draft"

    def test_refused_when_nothing_to_submit(self, client, workspace, user, account):
        post = _draft(workspace, user, account, status="pending_review")
        body = _call(client, "submit_for_review", {"post_id": str(post.id)})
        assert body["error"]["code"] == INVALID_PARAMS
        assert "no draft" in body["error"]["message"].lower()

    def test_refused_when_post_cannot_publish(self, client, workspace, user, instagram):
        post = _draft(workspace, user, instagram)
        body = _call(client, "submit_for_review", {"post_id": str(post.id)})
        assert body["error"]["code"] == INVALID_PARAMS
        assert post.platform_posts.get().status == "draft"

    def test_post_outside_allowlist_is_not_found(self, client, workspace, user):
        foreign = _account(workspace, suffix="foreign")
        post = _draft(workspace, user, foreign)
        body = _call(client, "submit_for_review", {"post_id": str(post.id)})
        assert body["error"]["message"] == "Post not found"


@pytest.mark.django_db
class TestCreateDraftSubmitFlag:
    def test_flag_lands_new_draft_in_review(self, client, account):
        inner = _inner(
            _call(
                client,
                "create_draft",
                {"social_account_id": str(account.id), "caption": "hi", "submit_for_review": True},
            )
        )
        assert inner["platform_posts"][0]["status"] == "pending_review"

    def test_default_still_creates_plain_draft(self, client, account):
        inner = _inner(_call(client, "create_draft", {"social_account_id": str(account.id), "caption": "hi"}))
        assert inner["platform_posts"][0]["status"] == "draft"

    def test_refused_submission_keeps_the_draft(self, client, workspace, account):
        workspace.approval_workflow_mode = "none"
        workspace.save(update_fields=["approval_workflow_mode"])
        body = _call(
            client,
            "create_draft",
            {"social_account_id": str(account.id), "caption": "keep me", "submit_for_review": True},
        )
        assert body["error"]["code"] == INVALID_PARAMS
        assert Post.objects.filter(caption="keep me", platform_posts__status="draft").exists()


@pytest.mark.django_db
class TestRestSubmitRoute:
    def test_submit_route(self, client, workspace, user, account):
        post = _draft(workspace, user, account)
        r = client.post(f"/api/v1/posts/{post.id}/submit")
        assert r.status_code == 200, r.content
        assert r.json()["platform_posts"][0]["status"] == "pending_review"

    def test_submit_route_conflict_without_workflow(self, client, workspace, user, account):
        workspace.approval_workflow_mode = "none"
        workspace.save(update_fields=["approval_workflow_mode"])
        post = _draft(workspace, user, account)
        r = client.post(f"/api/v1/posts/{post.id}/submit")
        assert r.status_code == 409
