"""
Roadmap-Routen (v6.7.26)
=========================
  GET  /roadmap                      — Public-Listenansicht mit Voting
  GET  /roadmap/new                  — Admin-Formular für neues Item
  POST /roadmap/new                  — Admin legt Item an
  GET  /roadmap/{id}/edit            — Admin-Edit-Formular
  POST /roadmap/{id}/edit            — Admin speichert Änderungen
  POST /roadmap/{id}/delete          — Admin löscht Item
  POST /roadmap/{id}/vote            — Eingeloggter User toggled Vote
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from roadmap import (
    STATUS_VALUES, CATEGORY_VALUES, PRIORITY_VALUES,
    list_items, get_item, create_item, update_item, delete_item,
    toggle_vote, get_user_votes, count_items_by_status,
    create_suggestion, approve_item, reject_item, count_pending,
)

logger = logging.getLogger("printix.web.roadmap")


def register_roadmap_routes(app: FastAPI, templates: Jinja2Templates,
                             t_ctx, require_login) -> None:

    def _user_or_redirect(request: Request):
        user = require_login(request)
        if not user:
            return None
        return user

    def _is_admin(user: Optional[dict]) -> bool:
        return bool(user and (user.get("is_admin") or user.get("role_type") == "admin"))

    @app.get("/roadmap", response_class=HTMLResponse)
    async def roadmap_index(request: Request):
        user = _user_or_redirect(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        status_filter = (request.query_params.get("status") or "").strip()
        category_filter = (request.query_params.get("category") or "").strip()
        is_admin = _is_admin(user)
        # v6.7.29: Admins sehen alle Pending, andere nur ihre eigenen
        items = list_items(
            status=status_filter,
            category=category_filter,
            include_pending=is_admin,
            viewer_user_id=user["id"],
        )
        user_votes = get_user_votes(user["id"])
        counts = count_items_by_status()
        pending_count = count_pending() if is_admin else 0
        flash = request.query_params.get("flash", "")
        return templates.TemplateResponse("roadmap.html", {
            "request": request, "user": user,
            "items": items,
            "user_votes": user_votes,
            "status_counts": counts,
            "status_filter": status_filter,
            "category_filter": category_filter,
            "status_values": STATUS_VALUES,
            "category_values": CATEGORY_VALUES,
            "priority_values": PRIORITY_VALUES,
            "is_admin": is_admin,
            "pending_count": pending_count,
            "flash": flash,
            **t_ctx(request),
        })

    # v6.7.29: User-Suggestion — normale User können Vorschläge einreichen,
    # Admin reviewed sie bevor sie öffentlich sichtbar werden.
    @app.get("/roadmap/suggest", response_class=HTMLResponse)
    async def roadmap_suggest_form(request: Request):
        user = _user_or_redirect(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        return templates.TemplateResponse("roadmap_suggest.html", {
            "request": request, "user": user,
            **t_ctx(request),
        })

    @app.post("/roadmap/suggest")
    async def roadmap_suggest_post(
        request: Request,
        title: str = Form(...),
        description: str = Form(""),
    ):
        user = _user_or_redirect(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if not title.strip():
            return RedirectResponse("/roadmap/suggest?flash=title_required",
                                     status_code=302)
        create_suggestion(
            title=title, description=description,
            submitted_by_user_id=user["id"],
        )
        return RedirectResponse("/roadmap?flash=suggested", status_code=302)

    @app.post("/roadmap/{item_id}/approve")
    async def roadmap_approve(request: Request, item_id: int):
        user = _user_or_redirect(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if not _is_admin(user):
            return RedirectResponse("/roadmap?flash=forbidden", status_code=302)
        approve_item(item_id)
        return RedirectResponse("/roadmap?flash=approved", status_code=302)

    @app.post("/roadmap/{item_id}/reject")
    async def roadmap_reject(request: Request, item_id: int):
        user = _user_or_redirect(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if not _is_admin(user):
            return RedirectResponse("/roadmap?flash=forbidden", status_code=302)
        reject_item(item_id)
        return RedirectResponse("/roadmap?flash=rejected", status_code=302)

    @app.get("/roadmap/new", response_class=HTMLResponse)
    async def roadmap_new_form(request: Request):
        user = _user_or_redirect(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if not _is_admin(user):
            return RedirectResponse("/roadmap?flash=forbidden", status_code=302)
        return templates.TemplateResponse("roadmap_edit.html", {
            "request": request, "user": user,
            "item": None,
            "status_values": STATUS_VALUES,
            "category_values": CATEGORY_VALUES,
            "priority_values": PRIORITY_VALUES,
            "is_new": True,
            **t_ctx(request),
        })

    @app.post("/roadmap/new")
    async def roadmap_create(
        request: Request,
        title: str = Form(...),
        description: str = Form(""),
        status: str = Form("idea"),
        category: str = Form("feature"),
        priority: str = Form("medium"),
        target_version: str = Form(""),
    ):
        user = _user_or_redirect(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if not _is_admin(user):
            return RedirectResponse("/roadmap?flash=forbidden", status_code=302)
        if not title.strip():
            return RedirectResponse("/roadmap/new?flash=title_required", status_code=302)
        create_item(
            title=title, description=description,
            status=status, category=category, priority=priority,
            target_version=target_version, created_by=user["id"],
        )
        return RedirectResponse("/roadmap?flash=created", status_code=302)

    @app.get("/roadmap/{item_id}/edit", response_class=HTMLResponse)
    async def roadmap_edit_form(request: Request, item_id: int):
        user = _user_or_redirect(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if not _is_admin(user):
            return RedirectResponse("/roadmap?flash=forbidden", status_code=302)
        item = get_item(item_id)
        if not item:
            return RedirectResponse("/roadmap?flash=not_found", status_code=302)
        return templates.TemplateResponse("roadmap_edit.html", {
            "request": request, "user": user,
            "item": item,
            "status_values": STATUS_VALUES,
            "category_values": CATEGORY_VALUES,
            "priority_values": PRIORITY_VALUES,
            "is_new": False,
            **t_ctx(request),
        })

    @app.post("/roadmap/{item_id}/edit")
    async def roadmap_update(
        request: Request,
        item_id: int,
        title: str = Form(...),
        description: str = Form(""),
        status: str = Form("idea"),
        category: str = Form("feature"),
        priority: str = Form("medium"),
        target_version: str = Form(""),
    ):
        user = _user_or_redirect(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if not _is_admin(user):
            return RedirectResponse("/roadmap?flash=forbidden", status_code=302)
        update_item(
            item_id=item_id,
            title=title, description=description,
            status=status, category=category, priority=priority,
            target_version=target_version,
        )
        return RedirectResponse("/roadmap?flash=updated", status_code=302)

    @app.post("/roadmap/{item_id}/delete")
    async def roadmap_delete(request: Request, item_id: int):
        user = _user_or_redirect(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if not _is_admin(user):
            return RedirectResponse("/roadmap?flash=forbidden", status_code=302)
        delete_item(item_id)
        return RedirectResponse("/roadmap?flash=deleted", status_code=302)

    @app.post("/roadmap/{item_id}/vote")
    async def roadmap_vote(request: Request, item_id: int):
        user = _user_or_redirect(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        result = toggle_vote(item_id, user["id"])
        logger.debug("Roadmap-Vote item=%s user=%s → %s",
                     item_id, user["id"], result)
        return RedirectResponse("/roadmap?flash=voted", status_code=302)
